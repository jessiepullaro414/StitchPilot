# StitchPilot roadmap

Where StitchPilot can go next. Each idea has a size (**S** about a day, **M** a few days, **L** a week or more) and a
reason it matters. The order inside each section is my recommendation.

## What it already does

Picture → stitches with fill, satin and bean stitch, underlay and pull compensation. AI assist, lettering (straight
or curved), monograms, motif and cross-stitch fills, AI text clean-up, appliqué with cut files, borders, a stitch editor, a complexity check with one-click simplify, density map, batch convert, worksheet, Cute
and Classic themes, and nine machine formats. It runs as a portable Windows exe or from source on macOS and Linux.

---

## 1. Next up: the biggest wins

| # | Idea | Size | Why |
|---|------|------|-----|
| 1 | ✅ **Done:** project files (`.stitchpilot`) with autosave, crash recovery and recent files. | M | Closing the app no longer loses work. |
| 2 | ✅ **Done:** shape-level editing. Click a shape to change its stitch type (fill / satin / outline), drag its fill-direction arrow, change density, sew it first or last in its colour, or remove it. Changes are saved in projects and re-stitch the design. *Later:* split or merge shapes, and edit several shapes at once. | L | The stitch editor fixes details; shape editing fixes the common "this area should be satin" or "wrong direction" problems in two clicks. |
| 3 | ✅ **Done:** fewer trims. Travel runs under areas that will be covered later; shapes that can reach each other are sewn as a group, and fills plan their exit. 25% fewer trims on the samples (logo 11 → 3), and visible shortcuts across finished stitches are gone. *Later:* better order for satin pieces within a letter. | M | Trims and jumps are the main thing that makes auto-digitized files feel amateur. |
| 4 | ✅ **Done:** better satin corners. Mitred corners, through-strokes at junctions with the other strokes stopping at their edge, short stitches on tight curves. Dense spots on bold lettering went from 4.3% to 0.6%. *Later:* Y- and K-type junctions where no stroke runs straight through. | M | Removes the bunched, lumpy corners on letters and outlines. |
| 5 | ✅ **Done:** machine profiles (format, thread chart, usual hoops). *Later:* more models, and per-model trim support once checked against manuals. | S | Removes the questions beginners get wrong most often. |
| 6 | ✅ **Done:** real-size placement template, tiled across pages for big designs. | S | The classic way to position a design on a shirt. |
| 7 | ✅ **Done:** AI text clean-up. Claude finds the words; StitchPilot tightens the box to the real letters, picks the installed font whose letter shapes overlap best, paints the old letters out and redraws them crisply. You check the words and font first. *Later:* curved text, and text on busy or patterned backgrounds. | M | Text in logos is where auto-digitizing looks worst. This makes it crisp. |

## 2. Stitch quality

| Idea | Size | Why |
|------|------|-----|
| ✅ **Done:** fill direction that follows each shape (along its length), with an arrow you can drag to change it | M | Fills catch the light more naturally and the fabric pulls less. |
| Edge-walk + tatami underlay for big fills, contour underlay for wide satins | S | Cleaner edges and better coverage on stretchy fabric. |
| Split satin for columns wider than the satin limit, instead of switching to fill | M | Keeps the satin sheen on bold letters and borders. |
| Pull compensation along the stitch direction only (not all round) | S | More accurate shapes, especially on knits. |
| Centreline tracing for line art and cartoon outlines (one clean satin or bean line per stroke) | M | Outlines stitch as smooth lines rather than many little pieces. |
| Auto outline: optional bean or satin line around every filled shape | S | The classic "cartoon patch" look in one tick. |
| Real embroidery fonts (pre-digitized satin alphabets) alongside system fonts | L | Small text (under 6 mm) only looks good with purpose-made embroidery fonts. |

## 3. Creative features

| Idea | Size | Why |
|------|------|-----|
| ✅ **Done:** **text on a curve** (arch or smile, with a live preview), plus **monograms** (1–3 letters, the middle one bigger, in five frame styles). *Later:* text that wraps round a monogram's frame, and more frame styles (laurel, banner). | M | Names arched over a logo and monograms are among the most-made home designs. |
| ✅ **Done:** **motif fills** (hearts, stars, flowers, strung like bunting), a **wavy fill**, and **cross-stitch fill**, picked per shape. *Later:* your own motif from a picture, and choosing the motif size directly. | M | Very cute, and a feature people buy software for. |
| **Photo stitch / sketch mode:** turn photos into running-stitch hatching or colour-blended fills | L | The answer to "can I embroider my dog?". It looks hand-drawn rather than posterised. |
| ✅ **Done:** **appliqué cut files** as SVG (Cricut, CanvasWorkspace) and DXF (ScanNCut, Silhouette), one layer per fabric, at real size. *Later:* FCM for ScanNCut, and an optional cutting allowance. | S | Machine-cut appliqué pieces fit perfectly. Big time saver. |
| **Layout canvas:** combine several designs and text, then move, scale and rotate them in the hoop | L | Lets people build a whole patch or shirt design in one place. |
| **Split large designs across several hoopings**, with alignment marks | L | Jacket backs and quilts. Today only bigger hoops can do that. |
| Garment mock-up: preview the design on a T-shirt, cap, tote or towel | M | Helps decide size and colours before stitching. |
| Free-standing lace and 3D puff modes | L | Specialist techniques. Later, but people love them. |

## 4. AI

| Idea | Size | Why |
|------|------|-----|
| **AI review:** Claude looks at the rendered stitch preview and points out gaps, crowded areas or odd ordering, with a "fix it" button for each | M | A second pair of expert eyes before wasting fabric. |
| **Plain-language changes:** "make the hair less dense", "drop the stars in the background", "make the border gold" | M | No need to know what pull compensation means. |
| ✅ **Done:** per-shape stitch suggestions. Claude proposes satin vs fill and a fill angle for each shape, with reasons; you pick which to use | M | Works together with shape-level editing (#2). |
| ✅ **Done:** **Create a picture with AI** (OpenAI image models): describe an idea and get embroidery-ready artwork in a chosen style and colour count. *Later:* edit a picture with a follow-up request ("make the cup red"), and the same on the web app. | M | Many people have an idea but no picture. |
| ✅ **Done (not live yet):** **StitchPilot Plus**, the AI subscription ($7.99/month: 60 AI pictures + 300 other AI actions; 3 + 10 free to try). Accounts and billing on stitchpilot.jessiescars.com, AI through StitchPilot's own gateway, no API keys in the app. See deploy/aws/SUBSCRIPTION.md. *Later:* the same for the web app, yearly plan, top-up packs. | L | Pays for the AI and makes it effortless (no keys to set up). |
| ✅ **Done (not live yet):** **Over-the-air updates.** The app checks a signed `latest.json` daily; the Windows exe downloads, checks and swaps itself in, then restarts; Macs get a download link. See deploy/UPDATES.md. *Later:* Mac in-place updates, code signing. | M | People stay on the latest version without re-downloading. |
| Fabric-aware thread colours: suggest threads that pop on the chosen fabric colour | S | Avoids navy-on-black disasters. |

## 5. Ease of use

| Idea | Size | Why |
|------|------|-----|
| First-run tour (three or four friendly tips) and a sample gallery to try | S | Lowers the "where do I start" moment. |
| Recent files, and drag-and-drop of several pictures for batch convert | S | Small conveniences people expect. |
| **My threads:** record the spools you own, and snap colours only to those | M | Never pick a colour you don't have. |
| More thread brands (Madeira, Isacord, Robison-Anton, Sulky) | M | Needs accurate, properly licensed colour charts. |
| Save straight to a USB stick (detect it, offer it as the save location) | S | Most home machines load designs from USB. |
| ✅ **Done:** cute dark theme (*Midnight*) and more pastel themes (*Mint*, *Lavender*). *Still to do:* interface translations, full keyboard and screen-reader support | M | More people can use it comfortably. |
| Undo for settings changes, with a history list | M | Experimenting feels safe. |

## 6. Getting it to people

| Idea | Size | Why |
|------|------|-----|
| ✅ **Done:** **web app**: the same engine in the browser (`python -m stitchpilot.web`, or the Docker image), ready to put online with upload limits, rate limits and bring-your-own Claude key. Projects open in both apps. Also ready for AWS (Amplify page + Lambda + S3, pay per use; guide in `deploy/aws`), not deployed yet. *Later:* stitch editor and batch convert on the web, accounts. | L | Works on any device, nothing to install. |
| **Automatic builds** (GitHub Actions): Windows exe plus Apple Silicon and Intel Mac apps, with the test suite on every change | M | No more building by hand on each machine. |
| Code signing: a Windows certificate (removes the SmartScreen warning) and Apple Developer ID notarization (removes the Gatekeeper warning) | S + cost | Strangers can't trust an app with a scary first launch. Both certificates cost money each year. |
| Installers (MSI / DMG) and an in-app "update available" check | M | Easy installs and easy updates. |
| Linux AppImage | S | `run.sh` already works; this makes it one file. |

## 7. Quality checks

| Idea | Size | Why |
|------|------|-----|
| **Real sew-outs** on a few machines (Brother, Janome, Bernina) for a set of test designs, logging what needed fixing | M | The only true test. Nothing has been stitched on a real machine yet. |
| Reference-image regression tests: render each sample's stitches and compare against approved images | S | Catches quality regressions automatically. |
| Speed tests on large, detailed pictures; progress and a cancel button for long conversions | S | Keeps it snappy on big designs. |
| A log file and a "copy diagnostics" button | S | Makes bug reports useful. |

---

## Suggested order

1. ✅ **Keep work safe:** project files and autosave (#1), machine profiles (#5), placement template (#6).
2. ✅ **Look professional:** fewer trims (#3), satin corners (#4), shape-following fill direction. Still to do:
   real sew-outs to confirm.
3. ✅ **Edit like a pro:** shape-level editing (#2) and AI per-shape suggestions.
4. ✅ **Delight:** text on a curve and monograms, motif fills, appliqué cut files, AI text clean-up (#7).
   Still to do: real sew-outs of the motif and cross-stitch fills, and a real-API check of text clean-up.
5. **Ship:** automatic builds, signing, installers.
