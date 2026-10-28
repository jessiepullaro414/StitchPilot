# Switching on StitchPilot Plus

StitchPilot Plus has three parts. All three are built and tested on a PC, but none is live yet:

| Part | Where | What it does |
|---|---|---|
| **Shop** | `car-site` repo, branch `stitchpilot-plus` (stitchpilot.jessiescars.com) | Accounts, the $7.99/month Stripe checkout, billing, "Connect this computer", and the AI allowance counters |
| **AI gateway** | `gateway/` + `deploy/aws/gateway.yaml` (AWS Lambda) | Holds your Claude and OpenAI keys; checks each request's allowance with the shop, then calls the AI |
| **Desktop app** | `stitchpilot/account.py` | Connects to an account and sends AI requests through the gateway |

The allowances live in the shop (`ALLOWANCE` in `src/lib/stitchpilot.ts`): Plus gets 60 AI pictures and 300 other
AI actions per calendar month, and everyone else gets 3 and 10 to try, once.

Do the steps in this order. Each one needs only what the earlier ones made.

## 1. Make a shared secret

The gateway proves to the shop that it's the gateway with a long random secret. Make one and keep it somewhere
safe (a password manager):

```
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## 2. The shop

In the `car-site` repo:

1. Review and merge the `stitchpilot-plus` branch. It adds:
   - the `/stitchpilot` pages and API routes, plus `lib/stitchpilot.ts`;
   - three tables and two `users` columns (migration `drizzle/0012_stitchpilot_plus.sql`, additive only);
   - routing of StitchPilot subscriptions in the Stripe webhook;
   - `?next=` support on sign-in and sign-up.
2. **Amplify ▸ Environment variables:** add `STITCHPILOT_GATEWAY_SECRET` = the secret from step 1. `amplify.yml`
   already passes it to the build.
3. **Run the migration** against the production database (it only adds tables and columns):
   `npm run db:migrate` (with `DATABASE_URL` set to production, as for earlier migrations).
4. **Amplify ▸ Hosting ▸ Custom domains:** add the subdomain `stitchpilot` on jessiescars.com, pointing at the
   same branch as the main site. Amplify adds the certificate. If DNS isn't in Route 53, add the CNAME it shows.
5. Deploy. Then check:
   - https://stitchpilot.jessiescars.com shows the StitchPilot Plus page;
   - signing in works there.

**Stripe needs no changes.** The checkout creates its price inline (like the clan tiers), and the existing
webhook already receives `checkout.session.completed` and `customer.subscription.*`. StitchPilot subscriptions
are tagged `product: stitchpilot_plus`, so a member's clan tier and Plus never affect each other. If the billing
portal is set up to show only certain products, add "StitchPilot Plus" there.

## 3. The AI gateway

You need the AWS CLI and SAM CLI, signed in to your AWS account (see [README.md](README.md), "What you need
first"; Docker isn't needed for this one). From the StitchPilot folder:

```
sam build --template-file deploy/aws/gateway.yaml
sam deploy --guided --stack-name stitchpilot-gateway
```

Answers:
- your Anthropic API key and OpenAI API key (these pay for the AI);
- the shared secret from step 1;
- `ShopUrl` = `https://stitchpilot.jessiescars.com`;
- `ReservedConcurrency` = 10, or -1 on a brand-new AWS account.

Note the `GatewayUrl` output.

## 4. The app

Put the gateway address in `stitchpilot/account.py` (`GATEWAY_URL`, without the trailing slash), or try it first
without rebuilding by setting the `STITCHPILOT_GATEWAY_URL` environment variable. Rebuild the exe
(`build\build_exe.ps1`).

## 5. Try it

1. In StitchPilot, choose **AI ▸ StitchPilot Plus & account ▸ Connect**, approve the code in the browser, and
   use a ✨ button: one free try is used.
2. Subscribe on stitchpilot.jessiescars.com. Use a 100% promotion code from the Stripe dashboard, or subscribe
   and refund yourself. Click **Refresh** in the account window: it should say Plus.
3. Cancel in **Manage subscription**. Once Stripe ends it, the account goes back to free.

## Costs

- **AI:** billed to your Anthropic and OpenAI accounts, per request. The allowances are sized so a member who uses
  all of them still costs well under $7.99, but check your providers' current prices against them.
- **Gateway:** a small Lambda (256 MB) charged only when used: cents a month for light use.
- **Shop:** no new hosting; it's the same Amplify app and database.
- Set an AWS Budget alert, and spending limits in the Anthropic and OpenAI consoles.

## Safety notes

- The app never holds AI keys. It holds a device token whose hash is all the shop stores. Members can disconnect
  computers on stitchpilot.jessiescars.com.
- The gateway only accepts StitchPilot's own kinds of request:
  - Claude: the app's model, at most 16,000 output tokens, no tools or streaming, at most 2 images;
  - pictures: the app's model, 1–4 per request, the app's sizes.
  Anything else is refused before any allowance is used or any AI is called.
- Allowance is counted atomically in the shop's database, so two requests can't both take the last free try.
- Failed AI calls are refunded automatically.
