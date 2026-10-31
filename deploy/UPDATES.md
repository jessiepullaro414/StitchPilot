# Releases and over-the-air updates

New users download StitchPilot from the repo's **GitHub Releases** page. Copies already in use update
themselves from the same place.

Copies of StitchPilot check for a newer version once a day, and also when someone chooses **Help ▸ Check for
updates…**. The automatic check can be turned off in the Help menu.

What happens when there is a newer version:
- **Windows (the portable exe):** the update window shows what's new. **Update and restart** downloads the new
  exe next to the old one, checks it, swaps it in and restarts. If someone has unsaved work and chooses to keep
  working, the new version starts the next time they open StitchPilot.
- **Mac or running from source:** the window offers **Download…** instead, which opens the Releases page.

## How it works

- Each release on GitHub has the app files (built by `.github/workflows/release.yml`) and a `latest.json`
  that you sign on your own computer.
- The app reads `https://github.com/jessiepullaro414/StitchPilot/releases/latest/download/latest.json`, which
  GitHub always points at the newest **published** release. Drafts and pre-releases never count.
- `latest.json` lists each file's download address. That address is the release's own versioned one, which
  never changes. It also lists each file's SHA-256 and size.

## How it's kept safe

- `latest.json` is signed with your private update key. The app only accepts it if the signature matches the
  public key built into the app (`PUBLIC_KEY` in `stitchpilot/updates.py`).
- The download is checked against the signed SHA-256 and size before it's used.
- The app only moves to a newer version, so an old release can't be replayed to roll people back.
- The signing key never goes near GitHub. Someone who took over your GitHub account still couldn't push a
  program to people's computers: only the key can do that.
- Before signing, the publish script checks each file's **build attestation**. That proves GitHub built it
  with this repo's release workflow, so you never sign something you didn't build.
- **Keep the key backed up.** A password manager is a good place. If you lose it, you have to make a new one,
  and copies of the app in use stop updating until people download the new key's version by hand.

## Set up (once, before the first release that people download)

1. **Make the signing key.** From the StitchPilot folder:
   ```
   python build/publish_update.py keygen
   ```
   This saves the private key to `%USERPROFILE%\.stitchpilot\update_signing_key.pem` and prints the public key.
   Back up the private key.
2. **Put the public key into the app:** paste it as `PUBLIC_KEY` in `stitchpilot/updates.py`, then commit and
   push.

That's all: there's no server to set up. Only exes built after step 2 can update themselves, so do it before
the first release you hand out.

## Each release

1. Raise `__version__` in `stitchpilot/__init__.py` (for example 0.1.0 → 0.2.0), commit and push.
2. Tag it, and GitHub tests and builds it:
   ```
   git tag v0.2.0
   git push origin v0.2.0
   ```
   You get a **draft** release with the Windows exe and the Mac apps (Apple Silicon and Intel). Only you can
   see a draft. If Mac signing is set up (`build/SIGNING.md`), approve the run when GitHub asks.
3. Sign it on your own computer:
   ```
   python build/publish_update.py release --github v0.2.0 --notes "What's new in this version"
   ```
   This downloads the draft's files, checks they were built by this repo's workflow, signs `latest.json` for
   them, and attaches it to the draft. It needs the GitHub CLI, signed in (`gh auth login`).
4. Look over the draft on GitHub, then click **Publish release**.
   - New users can download it from the Releases page straight away.
   - Copies in use offer the update within a day, or straight away when someone chooses **Check for
     updates…**.

To pull a bad release, delete it (or mark it a pre-release) on GitHub. "Latest" then goes back to the one
before, and copies that haven't updated yet stay where they are.

## Without GitHub

`deploy/aws/updates.yaml` makes an S3 bucket that can host the same files instead. To use it:

- set `STITCHPILOT_UPDATE_URL` (or `UPDATE_URL` in `updates.py`) to the bucket's `latest.json`;
- build the files yourself;
- publish them with `python build/publish_update.py release --notes "…" --exe … --mac … --upload s3://<bucket>`.

## Notes

- Windows SmartScreen doesn't block the swapped-in exe, because it isn't downloaded through a browser. People
  downloading StitchPilot for the first time still see SmartScreen until the exe is code-signed.
- If StitchPilot.exe sits in a folder the person can't change (such as Program Files), the update can't swap
  it. The window then explains how to download it by hand.
- Before the first signed release is published, the update check simply finds nothing.
