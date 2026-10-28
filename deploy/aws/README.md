# Putting StitchPilot on AWS

The web app runs as two pieces:

- **The API** is one AWS Lambda function (a container image) with a public *Function URL*, plus a private S3 bucket
  for uploaded pictures and cached designs. Everything in the bucket is deleted after one day.
- **The page** (HTML, JavaScript, CSS) is hosted by **AWS Amplify Hosting**, which gives it a free `https://...amplifyapp.com` address.

The browser loads the page from Amplify, calls the API at the Function URL, and uploads pictures straight to S3.
Visitors bring their own Claude API key for the AI features; it is sent with each AI request and never stored.

## What you need first

1. An AWS account, and the AWS CLI v2 set up with `aws configure` (do this yourself; this guide never asks for keys).
   The user you configure needs permission to create Lambda, S3, IAM roles, CloudFormation stacks and ECR repositories.
2. The [AWS SAM CLI](https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html).
3. **Docker Desktop, running** (SAM builds the container image with it).
4. Python 3.9 or newer (only for the small `build_site.py` helper; it needs no extra packages).

Run every command below from the project folder (the one that contains `Dockerfile.lambda`).

## Step 1: Deploy the API

```
sam build --template-file deploy/aws/template.yaml
sam deploy --guided
```

The first build downloads a few GB and takes a while. Answers for `sam deploy --guided`:

| Question | Answer |
|---|---|
| Stack Name | `stitchpilot` |
| AWS Region | the region you want, e.g. `us-east-1` |
| Parameter AllowedOrigin | `https://example.com` for now (you fix this in step 4) |
| Parameter ReservedConcurrency | `5` (see below if the deploy complains) |
| Confirm changes before deploy | your choice |
| Allow SAM CLI IAM role creation | `y` |
| Disable rollback | `n` |
| Create managed ECR repositories | `y` (the image needs somewhere to live) |
| Save arguments to samconfig.toml | `y` |

If it asks whether a function without authorization is OK, answer `y`: the function is public on purpose, it is a website.

**Brand-new AWS accounts** often start with a total Lambda limit of 10 concurrent runs, and AWS insists that at least 10 stay
unreserved, so reserving 5 fails. If you see that error, deploy with `ReservedConcurrency` set to `-1` (no reservation) and ask AWS
Support for a quota increase later.

When it finishes, print the outputs:

```
sam list stack-outputs --stack-name stitchpilot
```

Note down `FunctionUrl` (like `https://abc123.lambda-url.us-east-1.on.aws/`), `BucketName` and `Region`.
You can check the API works by opening `<FunctionUrl>api/health`; you should see `{"ok": true, ...}`.

## Step 2: Build the page

Use the values from step 1 (the trailing slash on the URL is fine, the script removes it):

```
python deploy/aws/build_site.py --api-url https://abc123.lambda-url.us-east-1.on.aws --bucket YOUR-BUCKET-NAME --region us-east-1
```

This creates `dist/site/` and `dist/site.zip`. The page is pointed at your API, and a `customHttp.yml` file carries the
security headers (a strict Content-Security-Policy that allows exactly your API and your bucket, no framing, no sniffing).

## Step 3: Upload the page to Amplify (manual, no Git needed)

1. Open the [Amplify console](https://console.aws.amazon.com/amplify/) (same region is fine) and choose **Create new app**.
2. Choose **Deploy without Git**, then **Next**.
3. App name `stitchpilot`, branch name `main`, method **Drag and drop**. Drop `dist/site.zip` and choose **Save and deploy**.
4. When it finishes you get an address like `https://main.d123abc.amplifyapp.com`. Copy it, with no trailing slash.

To update the page later, build it again and upload the new zip with **Deploy updates**.

### Alternative: deploy from Git

If the project is in a Git repository (GitHub, GitLab, ...): in Amplify choose **Host web app**, connect the repo and branch, and
Amplify will use the `amplify.yml` in the repo root. Before the first build, add these under **Hosting > Environment variables**:

- `STITCHPILOT_API_URL` = the FunctionUrl (no trailing slash)
- `STITCHPILOT_BUCKET` = the BucketName
- `STITCHPILOT_REGION` = e.g. `us-east-1`

The build writes `customHttp.yml` for the headers. If you open the site and the headers are missing (check in your browser's developer tools,
Network tab), paste the contents of `dist/site/customHttp.yml` into **Hosting > Custom headers** in the console and redeploy.

## Step 4: Tell the API which site may use it

Before this step the browser blocks the page's calls to the API (CORS), because the stack still has the placeholder address.
Redeploy with your real Amplify address (no image rebuild needed; this only changes settings):

```
sam deploy --parameter-overrides AllowedOrigin=https://main.d123abc.amplifyapp.com ReservedConcurrency=5
```

Now open your Amplify address, upload a picture, and convert it. Done.

*Order in short:* API with a placeholder address, then page on Amplify (which tells you its address), then API again with the real address.
The page does not need the Amplify address, so there is no circular dependency.

## Everyday tasks

- **Logs:** `sam logs --stack-name stitchpilot --tail`, or CloudWatch in the console, log group of the function (kept 14 days).
  The app logs tracebacks only, never requests or API keys.
- **Update the app after code changes:** `sam build --template-file deploy/aws/template.yaml` then `sam deploy`.
  The Function URL stays the same, so the page does not need rebuilding.
- **Allow more simultaneous conversions:** `sam deploy --parameter-overrides AllowedOrigin=<your amplify address> ReservedConcurrency=10`.
  Each conversion uses about 2 vCPUs for up to a few minutes; the value is also your cost and abuse ceiling. `0` switches the app off.
  (`--parameter-overrides` replaces the saved values, so always repeat `AllowedOrigin`.)
- **Tear everything down:**
  1. `aws s3 rm s3://YOUR-BUCKET-NAME --recursive` (the stack can't delete a bucket that still has files in it)
  2. `sam delete --stack-name stitchpilot` (answer yes to also delete the ECR image repository)
  3. In the Amplify console, open the app, **App settings > General settings > Delete app**.

## Rough monthly cost

These are estimates for light use (a handful of people, a few hundred conversions a month), not quotes. Prices change by region
and over time; check the official pages: [Lambda](https://aws.amazon.com/lambda/pricing/), [S3](https://aws.amazon.com/s3/pricing/),
[ECR](https://aws.amazon.com/ecr/pricing/), [Amplify Hosting](https://aws.amazon.com/amplify/pricing/), [CloudWatch](https://aws.amazon.com/cloudwatch/pricing/).

| Part | Roughly |
|---|---|
| Lambda | pay per use: a 20-second conversion at 3 GB is about 60 GB-seconds, a tenth of a cent or less. A thousand conversions is about a dollar, and Lambda's free tier (400,000 GB-seconds a month) covers a lot of that. |
| ECR (the image, a few GB) | tens of cents per month; usually the biggest fixed cost |
| S3 | cents (files vanish after a day) |
| Amplify Hosting | cents for a small static site (charged per GB stored, per GB served and per build minute) |
| CloudWatch logs | cents |

Expect somewhere around **$0.50 to $3 a month** when lightly used, more if the site gets popular. Set up an
[AWS Budget](https://console.aws.amazon.com/billing/home#/budgets) alert so you hear about surprises.

## Security notes

- Visitors use **their own Claude API key**. It travels in a request header to your function, is used for that one request and is never stored or logged.
  Your own key is never in the deployment.
- The Function URL is public (no sign-in), so anyone with the address can call it. Several things limit the damage:
  reserved concurrency caps how many conversions run at once (and therefore the bill), uploads are limited to 20 MB, S3 is private,
  encrypted and emptied after a day, and CORS restricts *browsers* to your Amplify site (this doesn't stop scripts, which is why the cap matters).
- The app's own per-visitor rate limiting lives in memory inside each Lambda instance, so it is only a mild brake here. For real rate limiting
  put **AWS WAF** in front of the site (Amplify console: **Hosting > Firewall**) and consider a rate-based rule. Note WAF protects the
  Amplify page; to protect the Function URL itself you would put CloudFront (with WAF) in front of it, which this guide does not set up.
- The page's security headers (CSP, no framing, no sniffing, HSTS) come from `customHttp.yml`, built by `build_site.py`.
