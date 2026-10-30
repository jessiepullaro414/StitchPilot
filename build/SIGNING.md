# Signing the Mac app

Without signing, macOS blocks StitchPilot the first time someone opens it. They then have to go to
**System Settings ▸ Privacy & Security ▸ Open Anyway**, and most people won't find that. A signed and
**notarized** app (Apple has checked it and approved it) just opens.

GitHub Actions does the signing and notarizing on every release build (`.github/workflows/release.yml`)
once the secrets below are set. Until then the Mac builds are unsigned, with a warning on the run.

You need an Apple Developer Program membership ($99/year). Everything else below works from Windows with
Git Bash, which comes with Git and includes `openssl`.

## 1. Join the Apple Developer Program

Enrol at <https://developer.apple.com/programs/> with an Apple ID that has two-factor sign-in.

- As an **individual**, your own name is shown as the developer.
- As an **organisation**, the business name is shown. You need a free D-U-N-S number, and approval takes
  longer.

Approval usually takes a few hours to a couple of days. Your **Team ID** (10 characters) is under
Membership details.

## 2. Make the Developer ID certificate

This is the certificate for apps given out outside the App Store. Only the account holder can create
one. Work in a folder outside the StitchPilot repo, for example `%USERPROFILE%\apple-signing`.

```bash
mkdir -p ~/apple-signing && cd ~/apple-signing
openssl genrsa -out devid.key 2048
openssl req -new -key devid.key -out devid.csr -subj "/emailAddress=you@example.com/CN=Your Name/C=US"
```

1. On <https://developer.apple.com/account/resources/certificates/add>, choose **Developer ID Application**
   (G2 Sub-CA), upload `devid.csr`, and download the certificate (`developerID_application.cer`) into the
   same folder.
2. Download Apple's intermediate certificate, **Developer ID - G2**, from
   <https://www.apple.com/certificateauthority/> (`DeveloperIDG2CA.cer`) into the same folder.
3. Bundle them into a password-protected `.p12`. Choose a strong password and keep it in your password
   manager:

```bash
openssl x509 -inform DER -in developerID_application.cer -out devid.pem
openssl x509 -inform DER -in DeveloperIDG2CA.cer -out g2.pem
openssl pkcs12 -export -inkey devid.key -in devid.pem -certfile g2.pem -name "Developer ID Application" \
  -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1 -out devid.p12
```

The `PBE-SHA1-3DES` options use the encryption the macOS keychain can read.

## 3. Make the notary key

Notarizing sends the app to Apple to check. GitHub uses an App Store Connect API key to do it.

1. Go to <https://appstoreconnect.apple.com/access/integrations/api>, then **Team Keys ▸ +**.
2. Name it "StitchPilot notarize" with the **Developer** role.
3. Download the `.p8` file. You can only download it once.
4. Note the **Key ID** and the **Issuer ID** shown on that page.

## 4. Put them in GitHub

1. Create the `release` environment: repo **Settings ▸ Environments ▸ New environment ▸ `release`**.
   - Under **Required reviewers**, add yourself. Every release build then waits for your OK before it can
     touch the secrets.
   - Under **Deployment branches and tags**, choose **Selected** and allow `main` and tags `v*`.
2. Add the secrets to that environment. Run these from the `apple-signing` folder. `gh` reads each value
   from the file or prompt, so nothing is shown on screen or stored in your shell history:

```bash
base64 -w0 devid.p12 | gh secret set MACOS_CERT_P12 --env release -R jessiepullaro414/StitchPilot
gh secret set MACOS_CERT_PASSWORD --env release -R jessiepullaro414/StitchPilot      # asks for the .p12 password
base64 -w0 AuthKey_XXXXXXXXXX.p8 | gh secret set APPLE_API_KEY_P8 --env release -R jessiepullaro414/StitchPilot
gh secret set APPLE_API_KEY_ID --env release -R jessiepullaro414/StitchPilot         # asks for the Key ID
gh secret set APPLE_API_ISSUER --env release -R jessiepullaro414/StitchPilot         # asks for the Issuer ID
```

## 5. Tidy up

- Save `devid.p12` (with its password) and the `.p8` file in your password manager.
- Delete the `apple-signing` folder. Nothing in it belongs in the repo, and `.gitignore` blocks these file
  types anyway.

## 6. Try it

Run **Actions ▸ Release ▸ Run workflow** and approve it when GitHub asks. The Mac jobs should show
Apple's verdict at the end of the build: `source=Notarized Developer ID`. Download a zip from the run's
artifacts on a Mac. It should open with a double-click and no warning.

If Apple turns the app down, the build prints Apple's report. The usual cause is a library inside the
app that wasn't signed.

## If something leaks

- **Certificate.** Revoke it on developer.apple.com and make a new one (step 2). Copies people already
  have keep working.
- **Notary key.** Revoke it in App Store Connect and make a new one (step 3).

In both cases, update the secrets in GitHub.
