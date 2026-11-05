# CostPilot Agent

Point it at an AWS profile; get a cost-optimization report for EC2, EBS, RDS, MSK and S3. Read-only.

**Contents:** [Prerequisites](#prerequisites) · [Quick start](#quick-start) · [How it works](#how-it-works) · [What it checks](#what-it-checks) · [IAM](#iam-read-only) · [Troubleshooting](#troubleshooting) · [Known limitations](#known-limitations) · [Files](#files)

## Prerequisites

| You need | Check |
|---|---|
| **Python 3.10+** | `python --version` |
| **Claude Code CLI**, signed in with your organization account. The agent runs on it, so no API key is needed. | `claude --version` |
| **AWS CLI v2** and a **profile for the target account** using a **read-only** identity (see [IAM](#iam-read-only)). SSO profiles need a fresh login. | `aws sts get-caller-identity --profile <profile>` |
| **Cost Explorer enabled** in the account. Optional: Compute Optimizer opted in. | Billing console |
| Resources at least **30 days old** to be judged (see [What it checks](#what-it-checks)). | |
| Outbound HTTPS to AWS and Anthropic. | |

> **Approval:** tool output (resource IDs, bucket names, spend, metrics) is sent to the model. Confirm this is allowed under Hyland's AI Use Requirements before running on a production or customer account.

## Quick start

```
pip install -r requirements.txt
aws sso login --profile <profile>      # SSO profiles only
```

**Web UI** (opens http://127.0.0.1:8000):

```
python ui_server.py
```

- **Analysis** tab: pick profiles, click **Run analysis**, watch progress, read the report on the page.
- **Copilot** tab: a read-only chat about one account (for example "Why is MSK so expensive?"). It answers from live read-only checks and refuses requests to change anything.
- Stop the server with `Ctrl+C`.

**Command line:**

```
python agent.py --profile <profile>                  # one account
python agent.py --profile dev staging prod           # several accounts, one after another
python agent.py --profile <profile> --regions us-east-1,us-east-2
```

Options:
- `--regions`: default `auto` (regions with at least $10 of in-scope spend in 60 days).
- `--model`: override Claude Code's default.
- `--out-dir`: default `reports`.

**Output:** `reports/costpilot-<account-id>-<date>.html` and `.md`, one per account. A run takes a few minutes: the AWS checks take under a minute, the rest is Claude writing the report.

## How it works

```
agent.py / ui_server.py  →  Claude Code (model)  →  tools (tools.py)  →  boto3  →  AWS read-only APIs
```

- **Framework:** Claude Agent SDK. It starts your local Claude Code CLI and reuses its login and org policy.
- **Tools:** 14 plain Python/boto3 functions, exposed to Claude as in-process MCP tools. Claude chooses which to call and writes the report; it never sees AWS credentials.
- **Fixed scope:** the profile and regions are set when the tools are built. Claude Code's own Bash/Read/Write tools are disabled.

## What it checks

| Area | Checks |
|---|---|
| Spend | Cost by service (current vs previous 30 days) and by region and usage type |
| EC2 | 30-day CPU and network, stopped instances, Compute Optimizer |
| EBS | Unattached and gp2 volumes, 30-day IOPS on in-use volumes, old snapshots, Compute Optimizer |
| RDS | 30-day CPU, connections, free storage |
| MSK | 30-day broker CPU and disk (provisioned clusters) |
| S3 | 30-day Standard size trend, lifecycle rules, versioning, multipart cleanup (all buckets) |

**30-day rule:** a resource is only called idle or oversized if it is at least 30 days old and has about 30 daily CloudWatch datapoints. Anything else is reported as "Needs more data" and excluded from savings totals. Compute Optimizer findings (14-day lookback) are shown as unvalidated. Unattached volumes, gp2, snapshots and S3 configuration are point-in-time observations with no usage history, so the report says to confirm with the owner first.

## IAM (read-only)

```
sts:GetCallerIdentity
ce:GetCostAndUsage
cloudwatch:GetMetricStatistics
ec2:DescribeInstances, ec2:DescribeVolumes, ec2:DescribeSnapshots
rds:DescribeDBInstances
kafka:ListClustersV2, kafka:ListNodes
s3:ListAllMyBuckets, s3:GetBucketLocation, s3:GetLifecycleConfiguration, s3:GetBucketVersioning
compute-optimizer:GetEC2InstanceRecommendations, compute-optimizer:GetEBSVolumeRecommendations
```

A missing permission only affects that check; the report lists it under "Gaps". The code cannot verify that your identity is read-only, so use a read-only role.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ProfileNotFound` | List profiles: `aws configure list-profiles` |
| Expired token / SSO error | `aws sso login --profile <profile>` |
| `AccessDenied` on one check | Add the permission from the IAM list, or an explicit deny applies. The run continues. |
| Fails at start in `auto` regions | Cost Explorer is off. Enable it, or pass `--regions`. |
| `CLINotFoundError` or auth error | Install Claude Code, put `claude` on PATH, run `claude` once and sign in. |
| Startup warnings about managed settings or blocked MCP servers | Your org's Claude Code policy. Harmless to this agent. |
| UI: port 8000 in use | Another `ui_server.py` is running. Stop it, or set `COSTPILOT_PORT` to another port. |
| Empty EC2/EBS but large EC2 spend | Resources are in an unscanned region or were deleted. Use `--regions`. |

## Known limitations

- Report wording varies between runs; the tool data underneath is deterministic.
- No EC2 memory metrics, so CPU alone does not prove an instance can shrink.
- Serverless MSK is not analyzed. S3 read frequency is not measured.
- EBS records creation time, not detach time. IOPS are daily averages.
- Regions under $10 of spend are skipped in `auto` mode; up to 100 resources per type per region are checked.
- Cost Optimization Hub support exists in `tools.py` but is switched off.
- The agent and Copilot only recommend. They make no changes to the account; Copilot answers a change request with what to check first.
- Copilot answers can take up to a minute while it runs checks; at most 3 chats are open at once.

## Files

| File | Purpose |
|---|---|
| `agent.py` | CLI entry point and the analysis function the UI reuses |
| `ui_server.py`, `ui/index.html` | Local web UI (localhost only, one analysis at a time). Port 8000, or set `COSTPILOT_PORT`. |
| `copilot.py` | Read-only chat: multi-turn, same 14 tools, no write tools |
| `tools.py` | The 14 boto3 tools and the 30-day validation |
| `agent_api.py` | System prompt (used by both); also an API-key version of the agent that needs `ANTHROPIC_API_KEY` |
| `render.py` | Markdown to self-contained HTML |
| `reports/` | Generated reports (contain account data; treat as internal) |
