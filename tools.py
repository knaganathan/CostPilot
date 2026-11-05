"""Read-only AWS cost-optimization tools for RDS, EC2, MSK, EBS and S3, bound to a single profile.

The profile is fixed when the tools are built, so the model can never pick a
different account. Every call is a Describe/List/Get API; nothing mutates.

Usage-based conclusions need MIN_DAYS of history. Tools return status
"validated_30d" only when that much data exists; otherwise "insufficient_data"
or "not_validated" and no utilization numbers.
"""
import datetime as dt
import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor

import boto3
from anthropic import beta_tool
from botocore.exceptions import ClientError

WORKERS = 8     # concurrent AWS calls (per fan-out); modest to stay clear of API throttling
MAX_ITEMS = 25  # keep tool output small so the model's context stays useful
MIN_DAYS = 30   # no usage-based conclusion without at least this much history
MAX_CW_RESOURCES = 100  # per region, bounds CloudWatch calls

# Cost Explorer SERVICE values for the in-scope services (Aurora bills under RDS, EBS under EC2 - Other)
SCOPE_SERVICES = [
    "Amazon Elastic Compute Cloud - Compute",
    "EC2 - Other",
    "Amazon Relational Database Service",
    "Amazon Managed Streaming for Apache Kafka",
    "Amazon Simple Storage Service",
]
# Cost Optimization Hub resource types in scope (substring match)
SCOPE_COH_TYPES = ("Ec2", "Ebs", "Rds", "Aurora", "Msk", "S3")


def _dump(obj) -> str:
    return json.dumps(obj, default=str, indent=1)


def _age_days(ts) -> int:
    return (dt.datetime.now(dt.timezone.utc) - ts).days


def _metric(cw, namespace, name, dims, stats):
    """Daily datapoints over the last MIN_DAYS+1 days (covers a full 30 days).

    Returns None if fewer than MIN_DAYS days of datapoints exist, so callers can
    report 'insufficient data' instead of concluding from a short window.
    """
    end = dt.datetime.now(dt.timezone.utc)
    resp = cw.get_metric_statistics(
        Namespace=namespace, MetricName=name,
        Dimensions=[{"Name": k, "Value": v} for k, v in dims.items()],
        StartTime=end - dt.timedelta(days=MIN_DAYS + 1), EndTime=end,
        Period=86400, Statistics=stats)
    pts = resp["Datapoints"]
    return pts if len(pts) >= MIN_DAYS - 2 else None  # tolerate small gaps


def _avg(pts, key):
    return round(sum(p[key] for p in pts) / len(pts), 1)


def discover_regions(profile: str, lookback_days: int = 60, min_cost: float = 10.0) -> list[str]:
    """Regions with at least min_cost USD of in-scope spend in the last N days (includes regions whose
    resources were since deleted). The threshold keeps trivial regions (cents of S3 or transfer) out of the scan."""
    ce = boto3.Session(profile_name=profile).client("ce", region_name="us-east-1")
    end = dt.date.today()
    resp = ce.get_cost_and_usage(
        TimePeriod={"Start": (end - dt.timedelta(days=lookback_days)).isoformat(), "End": end.isoformat()},
        Granularity="MONTHLY", Metrics=["UnblendedCost"],
        Filter={"Dimensions": {"Key": "SERVICE", "Values": SCOPE_SERVICES}},
        GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}])
    cost = {}
    for period in resp["ResultsByTime"]:
        for g in period["Groups"]:
            cost[g["Keys"][0]] = cost.get(g["Keys"][0], 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
    found = [r for r, c in sorted(cost.items(), key=lambda kv: -kv[1])
             if c >= min_cost and re.fullmatch(r"[a-z]{2}(-[a-z]+)+-\d", r)]
    return found or ["us-east-1"]


def make_tools(profile: str, regions: list[str]):
    session = boto3.Session(profile_name=profile)
    _clients, _lock = {}, threading.Lock()

    def client(service, region_name=None):
        """Cached boto3 client. boto3 Sessions are not thread-safe, so creation is serialized;
        the clients themselves are safe to share across threads."""
        with _lock:
            key = (service, region_name)
            if key not in _clients:
                _clients[key] = session.client(service, region_name=region_name)
            return _clients[key]

    def pmap(fn, items):
        """Run fn over items concurrently (I/O bound AWS calls), keeping input order."""
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            return list(pool.map(fn, items))

    def ec2(region):
        return client("ec2", region_name=region)

    def _scan(fn):
        """Run fn(region) for every region in parallel, tolerating per-region failures."""
        def one(r):
            try:
                return fn(r)
            except Exception as e:  # noqa: BLE001 - report, don't crash the agent
                return f"error: {e}"
        return _dump(dict(zip(regions, pmap(one, regions))))

    # ---------------------------------------------------------------- account / cost

    @beta_tool
    def get_account_identity() -> str:
        """Return the AWS account ID and ARN the agent is operating in."""
        ident = client("sts").get_caller_identity()
        return _dump({"account": ident["Account"], "arn": ident["Arn"]})

    @beta_tool
    def get_cost_by_service(days: int = 30) -> str:
        """Unblended cost for EC2, EBS (EC2 - Other), RDS, MSK and S3 over the last N days (minimum 30), plus the prior window for comparison.

        Args:
            days: Size of each window in days (default 30, values below 30 are raised to 30).
        """
        days = max(days, MIN_DAYS)
        ce = client("ce", region_name="us-east-1")
        end = dt.date.today()
        mid = end - dt.timedelta(days=days)
        start = mid - dt.timedelta(days=days)

        def window(s, e):
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": s.isoformat(), "End": e.isoformat()},
                Granularity="MONTHLY",
                Metrics=["UnblendedCost"],
                Filter={"Dimensions": {"Key": "SERVICE", "Values": SCOPE_SERVICES}},
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}],
            )
            totals = {}
            for period in resp["ResultsByTime"]:
                for g in period["Groups"]:
                    amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
                    totals[g["Keys"][0]] = totals.get(g["Keys"][0], 0) + amt
            return totals

        cur, prev = window(mid, end), window(start, mid)
        rows = [
            {"service": s, "current": round(c, 2), "previous": round(prev.get(s, 0), 2)}
            for s, c in cur.items()
        ]
        rows.sort(key=lambda r: r["current"], reverse=True)
        return _dump({
            "window_days": days,
            "total_current": round(sum(cur.values()), 2),
            "total_previous": round(sum(prev.values()), 2),
            "services": rows,
        })

    @beta_tool
    def get_cost_by_region_and_usage_type() -> str:
        """Last 30 days of EC2, EBS, RDS, MSK and S3 cost broken down by region and usage type (e.g. BoxUsage, NatGateway-Hours, DataTransfer). Use it to explain where spend comes from and what drives cost changes."""
        ce = client("ce", region_name="us-east-1")
        end = dt.date.today()
        resp = ce.get_cost_and_usage(
            TimePeriod={"Start": (end - dt.timedelta(days=MIN_DAYS)).isoformat(), "End": end.isoformat()},
            Granularity="MONTHLY", Metrics=["UnblendedCost", "UsageQuantity"],
            Filter={"Dimensions": {"Key": "SERVICE", "Values": SCOPE_SERVICES}},
            GroupBy=[{"Type": "DIMENSION", "Key": "REGION"}, {"Type": "DIMENSION", "Key": "USAGE_TYPE"}])
        rows = {}
        for period in resp["ResultsByTime"]:
            for g in period["Groups"]:
                k = tuple(g["Keys"])
                rows[k] = rows.get(k, 0) + float(g["Metrics"]["UnblendedCost"]["Amount"])
        top = sorted(rows.items(), key=lambda kv: -kv[1])[:MAX_ITEMS]
        return _dump([{"region": k[0], "usage_type": k[1], "cost": round(c, 2)} for k, c in top])

    @beta_tool
    def get_optimization_hub_recommendations() -> str:
        """Cost Optimization Hub recommendations for EC2, EBS, RDS/Aurora, MSK and S3 resources, by estimated monthly savings. AWS's own lookback applies; not validated by this agent."""
        coh = client("cost-optimization-hub", region_name="us-east-1")
        items, token = [], None
        while len(items) < 500:
            kwargs = {"maxResults": 100}
            if token:
                kwargs["nextToken"] = token
            try:
                resp = coh.list_recommendations(**kwargs)
            except ClientError as e:  # e.g. account not enrolled in Cost Optimization Hub
                return _dump({"status": "unavailable", "error": e.response["Error"]["Message"]})
            items += resp.get("items", [])
            token = resp.get("nextToken")
            if not token:
                break
        items = [i for i in items
                 if any(t in (i.get("currentResourceType") or "") for t in SCOPE_COH_TYPES)]
        items.sort(key=lambda i: i.get("estimatedMonthlySavings", 0), reverse=True)
        keep = ("actionType", "currentResourceType", "recommendedResourceType", "region",
                "resourceId", "estimatedMonthlySavings", "estimatedSavingsPercentage",
                "implementationEffort", "restartNeeded", "rollbackPossible")
        return _dump({
            "total_in_scope": len(items),
            "total_estimated_monthly_savings": round(
                sum(i.get("estimatedMonthlySavings", 0) for i in items), 2),
            "top": [{k: i.get(k) for k in keep} for i in items[:MAX_ITEMS]],
        })

    # ---------------------------------------------------------------- EC2

    @beta_tool
    def find_underused_ec2_instances() -> str:
        """Running EC2 instances with 30-day CPU and network utilization. Instances younger than 30 days or with <30 days of metrics are insufficient_data, never findings."""
        def scan(r):
            cw = client("cloudwatch", region_name=r)
            instances = []
            pages = ec2(r).get_paginator("describe_instances").paginate(
                Filters=[{"Name": "instance-state-name", "Values": ["running"]}])
            for page in pages:
                for res in page["Reservations"]:
                    instances += res["Instances"]
            instances = instances[:MAX_CW_RESOURCES]
            def check(i):
                row = {"id": i["InstanceId"], "type": i["InstanceType"],
                       "age_days": _age_days(i["LaunchTime"])}
                if row["age_days"] < MIN_DAYS:
                    row["status"] = "insufficient_data: younger than 30 days"
                    return row
                d = {"InstanceId": i["InstanceId"]}
                cpu = _metric(cw, "AWS/EC2", "CPUUtilization", d, ["Average", "Maximum"])
                net = _metric(cw, "AWS/EC2", "NetworkOut", d, ["Sum"])
                if cpu is None:
                    row["status"] = "insufficient_data: <30 days of CPU metrics"
                else:
                    row["status"] = "validated_30d"
                    row["cpu_avg_pct"] = _avg(cpu, "Average")
                    row["cpu_max_pct"] = round(max(p["Maximum"] for p in cpu), 1)
                    row["network_out_gb_30d"] = round(sum(p["Sum"] for p in net) / 1e9, 2) if net else None
                return row
            rows = pmap(check, instances)
            validated = sorted((x for x in rows if x["status"] == "validated_30d"),
                               key=lambda x: x["cpu_max_pct"])
            return {"checked": len(rows),
                    "insufficient_data": [x for x in rows if x["status"] != "validated_30d"][:MAX_ITEMS],
                    "lowest_utilization": validated[:MAX_ITEMS]}
        return _scan(scan)

    @beta_tool
    def find_stopped_instances() -> str:
        """Stopped EC2 instances with days since they were stopped (parsed from the state transition reason). They still pay for attached EBS. Only instances stopped 30+ days are validated."""
        def scan(r):
            out = []
            for res in ec2(r).describe_instances(
                    Filters=[{"Name": "instance-state-name", "Values": ["stopped"]}])["Reservations"]:
                for i in res["Instances"]:
                    m = re.search(r"\((\d{4}-\d{2}-\d{2})", i.get("StateTransitionReason", ""))
                    days = (dt.date.today() - dt.date.fromisoformat(m.group(1))).days if m else None
                    out.append({"id": i["InstanceId"], "type": i["InstanceType"],
                                "stopped_days": days,
                                "status": ("validated_30d" if days is not None and days >= MIN_DAYS
                                           else "insufficient_data: stop date unknown or <30 days")})
            return out[:MAX_ITEMS]
        return _scan(scan)

    @beta_tool
    def get_compute_optimizer_ec2_recommendations() -> str:
        """AWS Compute Optimizer EC2 rightsizing findings with the lookback each used. Lookback under 30 days = not_validated."""
        def scan(r):
            co = client("compute-optimizer", region_name=r)
            recs = co.get_ec2_instance_recommendations(maxResults=100).get("instanceRecommendations", [])
            rows = []
            for x in recs:
                opt = (x.get("recommendationOptions") or [{}])[0]
                look = x.get("lookBackPeriodInDays", 0)
                rows.append({
                    "instance": x["instanceArn"].split("/")[-1],
                    "finding": x.get("finding"),
                    "reasons": x.get("findingReasonCodes"),
                    "current": x.get("currentInstanceType"),
                    "recommended": opt.get("instanceType"),
                    "est_monthly_savings": ((opt.get("savingsOpportunity") or {})
                                            .get("estimatedMonthlySavings") or {}).get("value"),
                    "lookback_days": look,
                    "status": "validated_30d" if look >= MIN_DAYS else "not_validated: lookback <30 days",
                })
            rows.sort(key=lambda z: z["est_monthly_savings"] or 0, reverse=True)
            return rows[:MAX_ITEMS]
        return _scan(scan)

    # ---------------------------------------------------------------- EBS

    @beta_tool
    def find_unattached_ebs_volumes() -> str:
        """EBS volumes in 'available' state (not attached). Includes volume age; EBS does not record detach time, so unattached duration is not proven."""
        def scan(r):
            vols = ec2(r).describe_volumes(
                Filters=[{"Name": "status", "Values": ["available"]}])["Volumes"]
            return {"count": len(vols), "total_gb": sum(v["Size"] for v in vols),
                    "volumes": [{"id": v["VolumeId"], "size_gb": v["Size"], "type": v["VolumeType"],
                                 "age_days": _age_days(v["CreateTime"])} for v in vols[:MAX_ITEMS]]}
        return _scan(scan)

    @beta_tool
    def find_gp2_volumes() -> str:
        """gp2 EBS volumes that could be migrated to cheaper, faster gp3."""
        def scan(r):
            vols = ec2(r).describe_volumes(
                Filters=[{"Name": "volume-type", "Values": ["gp2"]}])["Volumes"]
            return {"count": len(vols), "total_gb": sum(v["Size"] for v in vols),
                    "sample": [v["VolumeId"] for v in vols[:MAX_ITEMS]]}
        return _scan(scan)

    @beta_tool
    def find_underused_ebs_volumes() -> str:
        """In-use EBS volumes with 30-day read/write IOPS (daily average). Flags idle volumes (zero I/O) and io1/io2/gp3 volumes whose provisioned IOPS far exceed use. Volumes younger than 30 days or with <30 days of metrics are insufficient_data."""
        def scan(r):
            cw = client("cloudwatch", region_name=r)
            vols = ec2(r).describe_volumes(
                Filters=[{"Name": "status", "Values": ["in-use"]}])["Volumes"][:MAX_CW_RESOURCES]
            def check(v):
                row = {"id": v["VolumeId"], "type": v["VolumeType"], "size_gb": v["Size"],
                       "provisioned_iops": v.get("Iops"), "age_days": _age_days(v["CreateTime"])}
                if row["age_days"] < MIN_DAYS:
                    row["status"] = "insufficient_data: younger than 30 days"
                else:
                    d = {"VolumeId": v["VolumeId"]}
                    rd = _metric(cw, "AWS/EBS", "VolumeReadOps", d, ["Sum"])
                    wr = _metric(cw, "AWS/EBS", "VolumeWriteOps", d, ["Sum"])
                    if rd is None and wr is None:
                        row["status"] = "insufficient_data: <30 days of metrics"
                    else:
                        total = sum(p["Sum"] for p in (rd or [])) + sum(p["Sum"] for p in (wr or []))
                        days = max(len(rd or []), len(wr or []))
                        row["status"] = "validated_30d"
                        row["total_ops_30d"] = int(total)
                        row["avg_iops"] = round(total / (days * 86400), 2)
                return row
            rows = pmap(check, vols)
            validated = sorted((x for x in rows if x["status"] == "validated_30d"),
                               key=lambda x: x["avg_iops"])
            return {"checked": len(rows),
                    "insufficient_data": [x for x in rows if x["status"] != "validated_30d"][:MAX_ITEMS],
                    "lowest_io": validated[:MAX_ITEMS]}
        return _scan(scan)

    @beta_tool
    def find_old_snapshots(older_than_days: int = 90) -> str:
        """Self-owned EBS snapshots older than N days (minimum 30).

        Args:
            older_than_days: Age threshold in days (default 90, raised to 30 if lower).
        """
        older_than_days = max(older_than_days, MIN_DAYS)
        cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=older_than_days)

        def scan(r):
            snaps = []
            for page in ec2(r).get_paginator("describe_snapshots").paginate(OwnerIds=["self"]):
                snaps += [s for s in page["Snapshots"] if s["StartTime"] < cutoff]
            return {"count": len(snaps), "total_gb": sum(s["VolumeSize"] for s in snaps),
                    "oldest_sample": [
                        {"id": s["SnapshotId"], "gb": s["VolumeSize"], "started": s["StartTime"]}
                        for s in sorted(snaps, key=lambda s: s["StartTime"])[:MAX_ITEMS]]}
        return _scan(scan)

    @beta_tool
    def get_compute_optimizer_ebs_recommendations() -> str:
        """AWS Compute Optimizer EBS volume findings with the lookback each used. Lookback under 30 days = not_validated."""
        def scan(r):
            co = client("compute-optimizer", region_name=r)
            recs = co.get_ebs_volume_recommendations(maxResults=100).get("volumeRecommendations", [])
            rows = []
            for x in recs:
                opt = (x.get("volumeRecommendationOptions") or [{}])[0]
                look = x.get("lookBackPeriodInDays", 0)
                rows.append({
                    "volume": x["volumeArn"].split("/")[-1],
                    "finding": x.get("finding"),
                    "current": x.get("currentConfiguration"),
                    "recommended": opt.get("configuration"),
                    "est_monthly_savings": ((opt.get("savingsOpportunity") or {})
                                            .get("estimatedMonthlySavings") or {}).get("value"),
                    "lookback_days": look,
                    "status": "validated_30d" if look >= MIN_DAYS else "not_validated: lookback <30 days",
                })
            rows.sort(key=lambda z: z["est_monthly_savings"] or 0, reverse=True)
            return rows[:MAX_ITEMS]
        return _scan(scan)

    # ---------------------------------------------------------------- RDS

    @beta_tool
    def find_idle_rds_instances() -> str:
        """RDS instances with 30-day CPU and connection stats. Younger than 30 days or <30 days of metrics = insufficient_data."""
        def scan(r):
            cw = client("cloudwatch", region_name=r)
            rows = []
            dbs = client("rds", region_name=r).describe_db_instances()["DBInstances"]
            for db in dbs[:MAX_CW_RESOURCES]:
                row = {"id": db["DBInstanceIdentifier"], "class": db["DBInstanceClass"],
                       "engine": db["Engine"], "multi_az": db["MultiAZ"],
                       "storage_type": db.get("StorageType"), "storage_gb": db.get("AllocatedStorage"),
                       "age_days": _age_days(db["InstanceCreateTime"])}
                if row["age_days"] < MIN_DAYS:
                    row["status"] = "insufficient_data: younger than 30 days"
                else:
                    d = {"DBInstanceIdentifier": db["DBInstanceIdentifier"]}
                    cpu = _metric(cw, "AWS/RDS", "CPUUtilization", d, ["Average", "Maximum"])
                    con = _metric(cw, "AWS/RDS", "DatabaseConnections", d, ["Maximum"])
                    free = _metric(cw, "AWS/RDS", "FreeStorageSpace", d, ["Minimum"])
                    if cpu is None or con is None:
                        row["status"] = "insufficient_data: <30 days of metrics"
                    else:
                        row["status"] = "validated_30d"
                        row["cpu_avg_pct"] = _avg(cpu, "Average")
                        row["cpu_max_pct"] = round(max(p["Maximum"] for p in cpu), 1)
                        row["max_connections_30d"] = max(p["Maximum"] for p in con)
                        if free and row["storage_gb"]:
                            min_free_gb = min(p["Minimum"] for p in free) / 1e9
                            row["min_free_storage_pct"] = round(100 * min_free_gb / row["storage_gb"], 1)
                rows.append(row)
            return rows[:MAX_ITEMS]
        return _scan(scan)

    # ---------------------------------------------------------------- MSK

    @beta_tool
    def find_underused_msk_clusters() -> str:
        """Provisioned MSK clusters with 30-day broker CPU and data-disk usage. Younger than 30 days or <30 days of metrics = insufficient_data."""
        def scan(r):
            kafka = client("kafka", region_name=r)
            cw = client("cloudwatch", region_name=r)
            rows = []
            clusters = kafka.list_clusters_v2()["ClusterInfoList"]
            for c in clusters[:MAX_CW_RESOURCES]:
                prov = c.get("Provisioned")
                row = {"name": c["ClusterName"], "type": c["ClusterType"], "state": c["State"],
                       "age_days": _age_days(c["CreationTime"])}
                if not prov:
                    row["status"] = "skipped: serverless (no broker sizing to optimize)"
                    rows.append(row)
                    continue
                ng = prov["BrokerNodeGroupInfo"]
                row.update({"instance_type": ng["InstanceType"], "brokers": prov["NumberOfBrokerNodes"],
                            "ebs_gb_per_broker": (ng.get("StorageInfo") or {}).get("EbsStorageInfo", {}).get("VolumeSize")})
                if row["age_days"] < MIN_DAYS:
                    row["status"] = "insufficient_data: younger than 30 days"
                    rows.append(row)
                    continue
                broker_ids = [str(int(n["BrokerNodeInfo"]["BrokerId"]))
                              for n in kafka.list_nodes(ClusterArn=c["ClusterArn"])["NodeInfoList"]
                              if "BrokerNodeInfo" in n][:12]
                cpu_user, cpu_sys, disk, missing = [], [], [], False
                for b in broker_ids:
                    d = {"Cluster Name": c["ClusterName"], "Broker ID": b}
                    u = _metric(cw, "AWS/Kafka", "CpuUser", d, ["Average", "Maximum"])
                    s = _metric(cw, "AWS/Kafka", "CpuSystem", d, ["Average"])
                    k = _metric(cw, "AWS/Kafka", "KafkaDataLogsDiskUsed", d, ["Maximum"])
                    if u is None or s is None or k is None:
                        missing = True
                        break
                    cpu_user.append(u); cpu_sys.append(s); disk.append(k)
                if missing or not broker_ids:
                    row["status"] = "insufficient_data: <30 days of broker metrics"
                else:
                    row["status"] = "validated_30d"
                    row["cpu_user_avg_pct"] = round(sum(_avg(u, "Average") for u in cpu_user) / len(cpu_user), 1)
                    row["cpu_system_avg_pct"] = round(sum(_avg(s, "Average") for s in cpu_sys) / len(cpu_sys), 1)
                    row["cpu_user_max_pct"] = round(max(p["Maximum"] for u in cpu_user for p in u), 1)
                    row["max_data_disk_used_pct"] = round(max(p["Maximum"] for k in disk for p in k), 1)
                rows.append(row)
            return rows[:MAX_ITEMS]
        return _scan(scan)

    # ---------------------------------------------------------------- S3

    @beta_tool
    def analyze_s3_buckets() -> str:
        """S3 buckets with 30-day Standard storage size/trend (CloudWatch daily metrics) and lifecycle, versioning and multipart-cleanup configuration. Buckets younger than 30 days or <30 days of size metrics are insufficient_data. Access frequency is NOT measured (request metrics are not enabled by default)."""
        s3 = client("s3")
        buckets = s3.list_buckets()["Buckets"]
        def check(b):
            name = b["Name"]
            row = {"bucket": name, "age_days": _age_days(b["CreationDate"])}
            try:
                region = s3.get_bucket_location(Bucket=name)["LocationConstraint"] or "us-east-1"
                row["region"] = region
                rules = []
                try:
                    rules = s3.get_bucket_lifecycle_configuration(Bucket=name)["Rules"]
                except ClientError as e:
                    if e.response["Error"]["Code"] != "NoSuchLifecycleConfiguration":
                        raise
                ver = s3.get_bucket_versioning(Bucket=name).get("Status", "Off")
                row["versioning"] = ver
                row["lifecycle_rules"] = len(rules)
                row["has_transition_or_expiration"] = any(
                    "Transitions" in x or "Expiration" in x for x in rules)
                row["has_abort_incomplete_multipart"] = any(
                    "AbortIncompleteMultipartUpload" in x for x in rules)
                row["has_noncurrent_version_expiration"] = any(
                    "NoncurrentVersionExpiration" in x for x in rules)
                if row["age_days"] < MIN_DAYS:
                    row["status"] = "insufficient_data: younger than 30 days"
                else:
                    cw = client("cloudwatch", region_name=region)
                    d = {"BucketName": name, "StorageType": "StandardStorage"}
                    pts = _metric(cw, "AWS/S3", "BucketSizeBytes", d, ["Average"])
                    if pts is None:
                        row["status"] = "insufficient_data: <30 days of size metrics (empty bucket or new)"
                    else:
                        pts.sort(key=lambda p: p["Timestamp"])
                        row["status"] = "validated_30d"
                        row["standard_gb_latest"] = round(pts[-1]["Average"] / 1e9, 2)
                        row["standard_gb_30d_ago"] = round(pts[0]["Average"] / 1e9, 2)
            except Exception as e:  # noqa: BLE001
                row["status"] = f"error: {e}"
            return row
        rows = pmap(check, buckets[:MAX_CW_RESOURCES])
        rows.sort(key=lambda x: x.get("standard_gb_latest", 0), reverse=True)
        return _dump({"total_buckets": len(buckets), "analyzed": len(rows),
                      "largest_standard": rows[:MAX_ITEMS]})

    return [
        get_account_identity,
        get_cost_by_service,
        get_cost_by_region_and_usage_type,
        # get_optimization_hub_recommendations is defined above but not registered: enable it
        # here once the account is enrolled in Cost Optimization Hub.
        # EC2
        find_underused_ec2_instances,
        find_stopped_instances,
        get_compute_optimizer_ec2_recommendations,
        # EBS
        find_unattached_ebs_volumes,
        find_gp2_volumes,
        find_underused_ebs_volumes,
        find_old_snapshots,
        get_compute_optimizer_ebs_recommendations,
        # RDS / MSK / S3
        find_idle_rds_instances,
        find_underused_msk_clusters,
        analyze_s3_buckets,
    ]
