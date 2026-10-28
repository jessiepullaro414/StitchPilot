# Over-the-air updates

Copies of StitchPilot check for a newer version once a day, and also when someone chooses **Help ▸ Check for
updates…**. The automatic check can be turned off in the Help menu.

What happens when there is a newer version:
- **Windows (the portable exe):** the update window shows what's new. **Update and restart** downloads the new
  exe next to the old one, checks it, swaps it in and restarts. If someone has unsaved work and chooses to keep
  working, the new version starts the next time they open StitchPilot.
- **Mac or running from source:** the window offers **Download…** instead.

## How it's kept safe

- Each release has a `latest.json` signed with your private update key. The app only accepts it if the
  signature matches the public key built into the app (`PUBLIC_KEY` in `stitchpilot/updates.py`).
- The signed information includes each file's SHA-256 and size. The download is checked against them before
  it's used.
- The app only moves to a newer version, so an old release can't be replayed to roll people back.
- Anyone who can change the hosted files still can't push a program to people's computers: only the private
  key can do that. **Keep the key off the server and backed up.** If you lose it, you have to replace it, and
  copies of the app in use stop updating until people download the new key's version by hand.

## Set up (once)

1. **Make the signing key.** From the StitchPilot folder:
   ```
   python build/publish_update.py keygen
   ```
   This saves the private key to `%USERPROFILE%\.stitchpilot\update_signing_key.pem` and prints the public key.
   Back up the private key (a password manager is a good place).
2. **Make the update site** (an S3 bucket: a few cents a month, plus about $0.09 per GB downloaded):
   ```
   aws cloudformation deploy --template-file deploy/aws/updates.yaml --stack-name stitchpilot-updates
   aws cloudformation describe-stacks --stack-name stitchpilot-updates --query "Stacks[0].Outputs"
   ```
   If the name `stitchpilot-updates` is taken, add `--parameter-overrides BucketName=<something unique>`.
3. **Put both into the app** in `stitchpilot/updates.py`:
   - `UPDATE_URL`: the stack's `UpdateUrl` output;
   - `PUBLIC_KEY`: the key from step 1.

   Only exes built after this step can update themselves, so do it before you hand out the next build.

## Each release

1. Raise `__version__` in `stitchpilot/__init__.py` (for example 0.1.0 → 0.2.0), commit and push.
2. Tag it, and GitHub builds it (`.github/workflows/release.yml`):
   ```
   git tag v0.2.0
   git push origin v0.2.0
   ```
   The tests run first. Then GitHub's machines build the Windows exe and the Mac apps (Apple Silicon and Intel)
   and put them in a **draft** release on GitHub, which only you can see.
3. Download the files and check that GitHub built them from this repository:
   ```
   gh release download v0.2.0 -D dist/v0.2.0
   gh attestation verify dist/v0.2.0/StitchPilot-0.2.0.exe -R jessiepullaro414/StitchPilot
   ```
   Do the same for each Mac zip.
4. Sign and upload on your own computer (the signing key never goes to GitHub):
   ```
   python build/publish_update.py release --notes "What's new in this version" --exe dist/v0.2.0/StitchPilot-0.2.0.exe --mac dist/v0.2.0/StitchPilot-0.2.0-mac-arm64.zip --mac dist/v0.2.0/StitchPilot-0.2.0-mac-x86_64.zip --upload s3://stitchpilot-updates
   ```
   The app files go up first and `latest.json` last, so the update information never points at a missing
   file. Leave out `--upload` to check `release/<version>/` before uploading.
5. If you want people to be able to download the files from GitHub too, publish the draft release.

You can still build by hand instead: `build\build_exe.ps1` on Windows, `build/build_mac.sh` on a Mac.

Running copies offer the update within a day, or straight away when someone chooses **Check for updates…**.

## Notes

- Windows SmartScreen doesn't block the swapped-in exe, because it isn't downloaded through a browser. People
  downloading StitchPilot for the first time still see SmartScreen until the exe is code-signed.
- If StitchPilot.exe sits in a folder the person can't change (such as Program Files), the update can't swap
  it. The window then explains how to download it by hand.
