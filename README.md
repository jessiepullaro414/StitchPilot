# StitchPilot

Turn a picture into a machine embroidery file in four steps: **open → size & fabric → look → save**.

![StitchPilot, cute theme](tests/out/theme_cute.png)

## Run it

### Windows

**Portable:** `dist\StitchPilot.exe` is a single file with no installer and no Python needed. Copy it anywhere (a USB
stick works) and double-click it, or drop a picture onto it. It keeps its settings in `StitchPilot.ini`
next to the exe. The first start takes a few seconds while it unpacks.

Rebuild the exe with `powershell -ExecutionPolicy Bypass -File build\build_exe.ps1`.

**From source:** double-click `StitchPilot.bat`. The first launch installs what it needs into
`%LOCALAPPDATA%\StitchPilot\venv`, a short path because Windows can't load some libraries from deep folders.

### macOS

You need Python 3.10 or newer, from [python.org](https://www.python.org/downloads/macos/) or `brew install python`.

**From source:** in Terminal, in this folder, run `bash run.sh` (add a picture path to open it straight away).
The first run sets up `~/Library/Application Support/StitchPilot/venv`, which takes a few minutes; later runs
start right away. `run.sh` works the same way on Linux.

**As a Mac app:** on a Mac, run `bash build/build_mac.sh`. It makes `dist/StitchPilot.app` (drag it to
Applications) and `dist/StitchPilot-mac-<arm64|x86_64>.zip` to share. The build also checks the finished app by
converting a sample picture.

- The app is built for that Mac's processor: Apple Silicon (`arm64`) or Intel (`x86_64`). Build on each kind
  you want to support.
- It's ad-hoc signed, so the first time, **right-click it ▸ Open ▸ Open** (or *System Settings ▸ Privacy &
  Security ▸ Open Anyway*). To remove that step, build with an Apple Developer ID:
  `SIGN_IDENTITY="Developer ID Application: …" NOTARY_PROFILE=<notarytool profile> bash build/build_mac.sh`.
  It then signs, notarizes and staples automatically.
- Settings are saved in the normal macOS preferences, not inside the app.
- Pictures and embroidery files can be dropped on the Dock icon or opened with *Open With ▸ StitchPilot*.
- On a trackpad, two-finger scroll pans; pinch or ⌘+scroll zooms. Shortcuts use ⌘ (⌘O, ⌘S, ⌘Z, ⌘E…).

### Web app

StitchPilot also runs in a web browser, on any computer, tablet or phone. It uses the same stitching engine as
the desktop app, and `.stitchpilot` projects open in both. The web app covers the main path: picture, size and
fabric, colours and Simplicity, curved lettering, monograms, borders, shape editing (including motif and
cross-stitch fills), threads (recolour, reorder, hide, appliqué), the AI features, and downloads (every machine
format, appliqué cut files, placement template PDF, preview image, project). The stitch editor, batch convert,
the worksheet and the complexity details stay desktop-only for now.

- **On your computer:** double-click `StitchPilot-Web.bat` (Windows), or run `python -m stitchpilot.web` after
  `pip install -r requirements-web.txt`. Open <http://localhost:8000>. Set `STITCHPILOT_HOST=0.0.0.0` to use it
  from a phone on the same Wi-Fi.
- **As a public website:** `docker build -t stitchpilot-web .` then `docker run -p 8080:8080 stitchpilot-web`,
  behind HTTPS (most hosts add it). Built for strangers to use:
  - Uploads are limited to 20 MB and 40 megapixels, and nothing is written to disk.
  - Each visitor is rate-limited, and the number of conversions running at once is capped
    (`STITCHPILOT_WORKERS`, default up to 4).
  - Strict security headers: scripts only from the site, no framing.
  - AI features use the **visitor's own Claude API key**. It is kept in their browser, sent only with AI
    requests, and never stored or logged by the server.
  - Behind a reverse proxy, set `STITCHPILOT_TRUST_PROXY=1` (the Docker image does) so rate limits see each
    visitor's address. Health check: `/api/health`.
- **On AWS, pay per use:** the page on AWS Amplify Hosting, the server as an AWS Lambda function (a container
  image) with a private S3 bucket. The API keeps no state between requests: uploads go from the browser straight
  to S3, and converted designs are cached there for a day. Step-by-step guide, template and cost notes:
  [deploy/aws/README.md](deploy/aws/README.md).

## What it does

| Step | How |
|---|---|
| **Reads** | PNG, JPG, WEBP, GIF, BMP, TIFF, HEIC/HEIF/AVIF, SVG, ICO, PSD (flattened)… |
| **Background** | Uses transparency if present; otherwise removes a plain background colour (including holes such as letter counters, unless you tick *Stitch background-coloured areas inside*). *Cut out subject* uses GrabCut, or the neural `rembg` matte if installed. |
| **Colours** | Edge-preserving smoothing, then colour reduction in CIELAB. *Automatic* picks the smallest palette that leaves no distinct colour unexplained and ignores anti-aliasing blends. |
| **Clean-up** | Majority filter + merging of specks too small to sew (size set by the *Detail* slider). |
| **Stitch types** | Each shape is measured with a distance transform and centreline: hair-thin lines → triple *bean* stitch; strokes up to the max satin width → *satin* columns that follow the stroke's direction; everything else → *tatami fill*. Satin that can't cover a shape falls back to fill. |
| **Satin corners** | Sharp corners (the points of V, W, A, M, Z…) are split into two columns that meet at a clean mitre instead of fanning into a lump. Where strokes meet (T, H, A, E, X), the straight-through stroke is sewn whole and the others stop at its edge, so junctions aren't sewn twice. Tight inner curves get short stitches. |
| **Fill direction** | Fill rows run along each shape's length; roundish shapes alternate angles per colour. *Fill angle* in the advanced settings switches to *Vary per colour* or a fixed angle. |
| **Fewer trims** | Travel between shapes of one colour runs underneath areas that a later colour, or a part of the same colour still to come, will cover. Shapes that can reach each other that way are sewn as a group, and each fill plans where to finish so the thread can leave without crossing its own stitches. If there's no hidden way, it trims rather than leave a visible line. About 25% fewer trims on the test pictures. |
| **Quality details** | Fill and satin underlay, pull compensation, staggered tatami pattern on a global grid, tie-in/tie-off locks, stitches capped at 7 mm, no sub-0.2 mm stitches. |
| **Order** | Large areas first, fine detail last, or the order you set with ▲▼ (hidden travel is re-planned to match); one thread change per colour. |
| **Writes** | PES (v6, keeps colours), DST, JEF, EXP, VP3, XXX, PEC, U01, TBF via pyembroidery, centred on the hoop. Optional thread-chart matching (Brother, Janome, Husqvarna Viking, generic). |

Fabric presets (woven, knit, towel, cap, light) set density, underlay and pull compensation.
Everything is adjustable under *Show advanced stitch settings*.

### More tools

| Feature | Where |
|---|---|
| **Projects (`.stitchpilot`):** one file holds the original picture (or embroidery file), every setting, lettering, appliqué and colour choices, and any hand stitch edits. Reopen it later exactly as you left it. The title shows • when there are unsaved changes, and StitchPilot asks before anything is lost. | *File ▸ Save project* (Ctrl+S), *💾 Save project* button, *Open recent* |
| **Autosave and recovery:** unsaved work is saved quietly every minute. If StitchPilot or the computer crashes, the next start offers to restore it. | automatic |
| **Machine profiles:** pick your machine (Brother, Baby Lock, Janome, Singer, Bernina, Husqvarna Viking/Pfaff, or commercial multi-needle) to set the file format, thread chart and the hoops it usually takes. The hoop lists are typical sizes, so check your manual. | *Size & fabric ▸ Machine* |
| **Real-size placement template (PDF):** the design at exactly 1:1 with centre crosshairs, a TOP mark, the hoop outline and a 50 mm / 2 in ruler to check the print scale. Designs bigger than the paper are split across pages with + marks for taping. Letter or A4. | *📐 Placement template*, *File ▸ Print placement template* |
| **Themes:** *Cute* (default) uses pastel pink and lavender, stitched-patch cards, pill buttons and emoji headings. *Mint* 🌿 and *Lavender* 💜 are the same cute style in fresh green and soft lilac. *Midnight* 🌙 is a cute dark theme (deep plum with pink). *Classic* is the original purple look. Every theme keeps its own colours whatever the system's light/dark setting, and the tests check each one's text contrast. | *View ▸ Theme* |
| **Complexity check:** every picture gets a score (*Simple / Moderate / Complex / Very complex / Photo*) with plain reasons: too many colours, soft shading, too many separate shapes, shapes too small to sew, hair-thin lines. The current result is scored next to it. | *Simplify* |
| **Simplify:** one *Simplicity* slider merges similar colours, absorbs specks and holes, thickens hair-thin lines and smooths edges. *More simplify options* exposes each control. *Show problem spots* marks trouble areas in red; *Compare with original* shows before and after side by side. | *Simplify* |
| **Make it embroidery-friendly:** finds the lightest simplification that reaches at most 8 colours, few tiny shapes and no hair-thin lines | *Simplify* |
| **Create a picture with AI:** no picture yet? Describe one ("a smiling sloth hugging a coffee cup"), pick a style (cute sticker, flat clipart, logo, patch, line art, retro), how many colours, quality and shape, and OpenAI's image model (`gpt-image-2.5-flare`) draws 1–4 versions. The request asks for artwork that embroiders well: flat colours, a small palette, bold shapes, no tiny details, on a transparent background. Pick one and it opens like any other picture; it's also saved as a PNG in *Pictures/StitchPilot AI*. Part of StitchPilot Plus: each version uses one AI picture from the allowance. | *✨ Create with AI* under the picture, *File ▸ Create a picture with AI* (Ctrl+Shift+N) |
| **Lettering:** names, initials or slogans in any installed font, placed above, below or on top of the picture. A lettering-only design keeps your letter height. **Curve** bends a line round a circle: + arches it over like a rainbow, − curves it under like a smile. The dialog shows a live preview. | *＋ Add text* in *Lettering & border*, or *File ▸ New lettering design* |
| **Monograms:** 1–3 initials (with three, the middle one is bigger, as in classic monograms) in a frame: circle, double circle, scalloped circle, diamond or rounded square. The frame is sewn as a satin line. | *＋ Monogram*, or *File ▸ New monogram* |
| **AI text clean-up:** Claude finds the words in the picture. StitchPilot then tightens each box to the real letters, measures the text and background colours, and picks the installed font whose letter shapes overlap best. It paints the blurry letters out and redraws them crisply before stitching. You check the words and font, then tick what to redraw. On a small, blurry JPEG of the sample logo, the text went from not stitching at all to clean letters. Curved text is listed but left as it is for now. | *✨ Clean up text in the picture* in *Lettering & border* |
| **Appliqué:** placement line, stop, tack-down, stop, then a satin cover edge. Other colours sew on top of the fabric piece. | ⋯ next to a thread |
| **Appliqué cut files:** the fabric pieces at real size, as SVG (Cricut Design Space, Brother CanvasWorkspace) or DXF (ScanNCut, Silhouette, most cutter software). Each fabric is its own group or layer, so a Cricut puts each fabric on its own mat. Pieces are cut to the placement line; the satin edge covers the cut edge. Lay a pre-cut piece on its placement line and skip the trimming stop. | *File ▸ Export appliqué cut file*, or ⋯ next to an appliqué thread |
| **Border:** a bean-stitch outline, or a satin patch edge just outside the design, with adjustable width and gap | *4 Lettering & border* |
| **Open embroidery files** (PES, DST, JEF, EXP, VP3, XXX, PEC, U01…) to view, recolour, reorder, check density and save as another format | *Open* or drop |
| **Density map**, with a warning when areas are dense enough to break needles or pucker fabric | *Density* tab |
| **Reorder and merge colours.** Recolouring one thread to match the one before it makes them a single thread. | ▲▼ / ⋯ next to a thread |
| **Batch convert** many pictures with the current settings | *File ▸ Batch convert pictures…* |
| **Printable worksheet (PDF)** with preview, size, stitches, sew time and colour sequence | *File ▸ Print worksheet* |
| **Preview on your fabric colour**, with the hoop outline and a play button for the sew-out | bar under the preview |
| **Jumps-only mode** for machines without a thread cutter | *My machine cuts threads automatically* |
| **Rotate and mirror** | under the picture |
| **Shape editing:** click a shape to change its stitch type (*Fill*, *Satin* or *Outline only*), drag its arrow (or type an angle) to turn the fill direction, make it denser or lighter, sew it first or last in its colour, or remove it (Del). Changed shapes get a dashed outline. Every change re-stitches the design, so hidden travel and corners stay right. Undo/redo with Ctrl+Z / Ctrl+Y, and changes are saved in projects. | *🧩 Edit shapes* (Ctrl+M) under the preview |
| **Decorative fills:** in shape editing, the *Stitch* list also has *Waves* (a full fill with rippling rows), *Hearts*, *Stars* and *Flowers* (rows of little bean-stitch motifs strung edge to edge like bunting, with an outline round the shape), and *Cross-stitch* (an X in every 2.2 mm square, each diagonal sewn there and back). Motif and cross-stitch fills are light and open, so the fabric shows through and nothing is hidden under them. *Density* makes the motifs or squares smaller or bigger. | *🧩 Edit shapes ▸ Stitch* |
| **AI shape suggestions:** Claude looks at every numbered shape and suggests a stitch type and fill direction where it would clearly sew better, with a short reason for each. Click a suggestion to see its shape, untick any you don't want, and apply the rest as one undoable step. | *✨ Suggest with AI* in shape editing |
| **Stitch editor:** select needle points (click, Shift-click, drag a box, or double-click for a whole run), then delete them or drag to move. The *Add stitches* tool sews from the selected point to each click, or starts a new run in a chosen colour. Undo/redo with Ctrl+Z / Ctrl+Y. Edits are kept when you change settings, resize, rotate or mirror: the design re-converts and they're put back in the same places (erased areas are cut out before stitching, so the stitches around them are redone cleanly). *Discard edits* goes back to the plain conversion. | *✎ Edit stitches* (Ctrl+E) under the preview |

## AI assist

*✨ Let AI choose settings* sends a downscaled copy of the picture to Claude (`claude-opus-5-5`), which
answers like a professional digitizer: which flat thread colours to use, what the background is, whether
background-coloured areas inside the design (white eyes, white lettering) should be stitched, the detail
level, the minimum sensible size and a few tips. The app applies those settings and you can still change
anything.

*Create with AI* draws pictures with OpenAI instead (Claude doesn't generate images). Requests to Claude use
server-side refusal fallback (`fallbacks: "default"`), so a declined request is retried on another model
automatically.

### StitchPilot Plus (the AI subscription)

All AI features in the desktop app come with **StitchPilot Plus** ($7.99 a month): 60 AI pictures and 300
other AI actions every month. Anyone can try them free with an account: 3 AI pictures and 10 other AI actions.
Nobody needs API keys of their own.

- **AI ▸ StitchPilot Plus & account** connects this computer to an account on
  [stitchpilot.jessiescars.com](https://stitchpilot.jessiescars.com) (part of the jessiescars.com shop: same
  accounts, Stripe checkout and billing page). The app shows a short code, you approve it in the browser, and
  it's linked. The account window shows your plan and how much AI is left.
- AI requests go to the **StitchPilot AI gateway** (`gateway/`, an AWS Lambda). It checks the account's
  allowance with the shop, calls Claude / OpenAI with StitchPilot's own keys, refunds the allowance if the AI
  call fails, and only accepts StitchPilot's own kinds of request (fixed models and limits).
- When the allowance runs out, the app says so and offers Plus. *Run AI automatically on new pictures* only
  runs for Plus members, so free tries aren't used up just by opening files.
- How to switch it on (shop, gateway, app): [deploy/aws/SUBSCRIPTION.md](deploy/aws/SUBSCRIPTION.md).

The web app (`stitchpilot/web`) still uses each visitor's own Claude key for now.

## Updates

StitchPilot checks for a newer version once a day (and on **Help ▸ Check for updates…**). The portable Windows
exe updates itself: **Update and restart** downloads the new exe, checks it, swaps it in and restarts. On a Mac
it offers the download. Updates come from the repo's GitHub Releases, and each release's `latest.json` is signed
with your own key, so only you can publish an update. Set-up and releasing: [deploy/UPDATES.md](deploy/UPDATES.md)
(`build/publish_update.py release --github vX.Y.Z`).

## Automated builds

GitHub Actions runs the tests on Windows and macOS for every push and pull request (`.github/workflows/ci.yml`).
Pushing a version tag (`git tag v0.2.0 && git push origin v0.2.0`) also builds the Windows exe and the Mac apps
for Apple Silicon and Intel. They're attached to a draft GitHub release (`.github/workflows/release.yml`).
To try the builds without releasing anything, use **Actions ▸ Release ▸ Run workflow**. The files stay with the
run for 14 days. Mac builds are signed and notarized once the Apple secrets are set ([build/SIGNING.md](build/SIGNING.md)).
Updates are still signed on your own computer, never on GitHub. Python packages and actions are pinned to
exact versions, and Dependabot suggests updates each week. See [SECURITY.md](SECURITY.md).

## What's next

See [ROADMAP.md](ROADMAP.md) for planned features and the suggested order.

## Tests

```
python tests/make_samples.py      # sample artwork
python tests/run_pipeline.py      # digitize, export every format, read back, check stitch lengths
python tests/features.py          # lettering, appliqué, borders, jumps-only, rotation, density, file read-back
python tests/gui_smoke.py         # drives every GUI feature offscreen (simulated dark mode), screenshots in tests/out
python tests/edit_smoke.py        # stitch editor with real mouse/keyboard events
python tests/edit_keep_smoke.py   # hand edits kept through density, size, rotate/mirror changes, undo and projects
python tests/simplify_smoke.py    # complexity score, problem map, Simplicity slider, one-click friendly
python tests/mac_bits.py          # Finder/Dock file-open events, trackpad pan vs. wheel zoom (runs on any OS)
python tests/project_smoke.py     # projects, hand edits, unsaved-changes prompt, autosave/recovery, machines, templates
python tests/quality_smoke.py     # mitred satin corners, travel hidden under later colours, shape-following fills
python tests/shape_smoke.py       # shape editing with real mouse/keys, AI shape suggestions (mocked), projects
python tests/delight_smoke.py     # curved text, monograms, motif/cross-stitch fills, cut files, AI text clean-up (mocked)
python tests/web_smoke.py         # the web app: real server, every endpoint, public-site limits, AI routes (mocked)
python tests/lambda_smoke.py      # the web app as AWS Lambda: Function URL events, a stand-in S3, direct uploads
python tests/imagegen_smoke.py    # Create with AI (OpenAI mocked): request, errors, dialog, picture opens and stitches
python tests/account_smoke.py     # StitchPilot Plus end to end: app -> real gateway -> fake shop + fake Claude/OpenAI
python tests/update_smoke.py      # updates: a signed release from publish_update.py, checks, download, swap, update window
python tests/quality_bench.py     # trims, jumps and density for every sample (--save / --compare a JSON baseline)
python tests/theme_shots.py       # every theme: screenshots in tests/out and a text-contrast check
python tests/run_all.py           # everything above, each with a time limit
python tests/ai_mock.py           # AI assist against a mocked API (no key needed)
```

## Layout

```
app.py                 entry point
stitchpilot/
  imageio.py           load any image (Pillow, pillow-heif, Qt SVG)
  prep.py              background removal, scaling, colour reduction, clean-up
  stitches.py          skeletons, junctions, tatami fill, satin with mitred corners, bean stitch, sequencer
  routing.py           hidden travel: routes under stitching still to come, fill exits, shape grouping
  digitize.py          region analysis, stitch-type choice, colour ordering -> Design
  export.py            pyembroidery writer + thread charts
  render.py            thread-shaded preview renderer (fabric colour, hoop overlay)
  analysis.py          stitch density map
  complexity.py        complexity score, problem map, embroidery-friendly search
  lettering.py         text rendering and placement, curved text, monograms and frames
  patterns.py          decorative fills: hearts, stars, flowers, waves, cross-stitch
  textfix.py           AI text clean-up: find the letters, match a font, redraw them
  cutfiles.py          appliqué cut files (SVG, DXF)
  imagegen.py          Create with AI: embroidery-friendly prompts for OpenAI's image models
  account.py           StitchPilot Plus: connect this computer, account info, AI clients via the gateway
  updates.py           over-the-air updates: signed latest.json, checked download, exe swap and restart
  web/                 the web app: server.py (routes, limits), engine.py (the desktop logic, no window),
                       jobs.py (worker-process work), storage.py (memory or S3), lambda_handler.py (AWS),
                       static/ (the page: HTML, CSS, JS, no build step)
deploy/aws/            AWS: web app template (Lambda + S3), AI gateway template, guides
gateway/               StitchPilot AI gateway (Lambda, standard library only): allowance check + Claude/OpenAI
  dialogs.py           lettering dialog, PDF worksheet
  editing.py           stitch edit operations (select, delete, move, insert, new run) and the edit log that
                       re-applies them after settings change
  project.py           .stitchpilot project files (zip: settings JSON, original source, edited stitches)
  machines.py          machine profiles (format, thread chart, usual hoops)
  template.py          real-size placement template PDF (tiled when larger than the paper)
  ai.py                Claude analysis -> settings
  theme.py             Cute / Classic themes (stylesheet, palette, generated tick and chevron icons)
  gui.py               PySide6 app
build/build_exe.ps1    portable single-file exe (PyInstaller)
build/build_mac.sh     StitchPilot.app + zip (PyInstaller, Info.plist file types, signing, optional notarization)
build/requirements-build.txt  PyInstaller version for both builds
.github/workflows/     ci.yml (tests on Windows + macOS), release.yml (version tag -> builds -> draft release)
.github/dependabot.yml weekly package and action updates
run.sh                 run from source on macOS / Linux
StitchPilot.bat        run from source on Windows
```
