<!--
The MakerWorld model page. Everything from the heading down to the *** rule is
what gets pasted into the description box; the notes under it are for us.

Shaped to match what the platform's own successful CYD listings do - read off
three of them on 2026-09-11, noted at the bottom. In short: open with why you
built it, then a features list, then what to buy, then print settings, then
assembly, then links out. They all repeat the print settings in the description
even though the profile carries them, because that is what a person checks
before clicking print.

Plain hyphens throughout, never -- or an em dash: nothing converts them, so
both characters just show.

THE DESCRIPTION BOX IS A WYSIWYG EDITOR, NOT A MARKDOWN FIELD. It has a
Paragraph dropdown, B/I/U, lists and a table button. Pasting this file raw puts
literal ## and ** on the page. Get formatting in by pasting rendered HTML from
a browser instead:

    sh tools/makerworld_paste.sh

which writes ~/Downloads/blink-makerworld-paste.html from the body below.
Open it, select from the heading down, copy, paste. Headings, bold and bullets
survive; MakerWorld restyles them to its own look.

Keeping this file as markdown is still right: it diffs, it renders on GitHub,
and the HTML is derived from it rather than the other way round.
-->

# BLINK

**Your Claude Code and Codex usage, as two live dials on your desk.**

Your 5-hour session and your 7-day week, each as a dial that runs green to
amber to red, with a countdown to when it resets. It reads the figures Claude
Code and Codex have already written on your own computer, so there is no
account, no sign-in, and nothing leaves the machine.

## What it does

- Two dials: the 5-hour session and the 7-day week, with the time until each
  one resets.
- A mark in the top-left corner for every coding session you have running,
  coloured by what it is doing.
- Claude and Codex each get a page of their own. Swipe between them.
- It dozes when your computer sleeps and wakes when it wakes.
- Updates itself over the same USB lead.

## What to buy

- **An ESP32-2432S028** - the "Cheap Yellow Display". 2.8 inch, 320x240, touch.
  Sold everywhere as a CYD, about $12.
- **A USB data cable.** Power and readings share the one lead, so a
  charge-only cable will not do.

Links are in the Bill of Materials. That is the whole shopping list: no screws,
no heat-set inserts, no magnets, and nothing to solder.

## Printing

Six parts on three plates, already laid out and oriented in the 3MF. Printed in
PLA on a P1S with a 0.4 nozzle, but there is nothing printer-specific about it.

- **Layer height** 0.2 mm
- **Material** PLA. The ones in the photos are Bambu PLA Basic, Pumpkin
  Orange (10301).
- **Walls** 2, with 5 top layers and 3 bottom
- **Infill** 30%, grid
- **Brim** auto, 5 mm

**Supports:** the front needs them and they are already painted on. The back
needs none, and the project has them switched off for it. **Leave them off.**
The stand prints in place in a shallow recess, and support material in that gap
is what welds it shut.

The three plates are the front, standing on its edge so the screen opening
rises vertically; the back, flat; and the four small screen spacers.

## Assembly

1. Drop the board into the front, screen first, so the display sits in the
   window.
2. Push the four spacers down over the posts at the corners of the board. They
   hold the screen against the front, so they go on before anything closes over
   them.
3. Fit the back on, lining the USB socket up with its opening. It only goes on
   one way round: the vent slots run across the top.

## The stand

The stand is part of the print. It folds flat into a recess in the back, so the
case lies flat in a bag, and swings out to hold the screen at an angle.

1. It prints folded away, and the back looks flat.
2. Hook a fingernail under the bar and slide it down until it is clear.
3. Swing it out from the back until it stops. It stops where it stops.
4. Stand it up. It folds back in the same way.

Gently. Nothing here needs force.

## Making it work

The firmware and the app are free and open source, and one command installs
both. Everything, including what to run on your computer:

**https://github.com/KfirLevy258/Blink**

## Would rather not build one?

I build them, test them and post them - flashed, assembled, and checked on a
real desk before they go out.

**https://blink-buddy.com**

## Licence

Print as many as you like, for yourself. Beyond that, please keep it here:
don't sell it, don't pass the printed cases on, and don't re-post the files
elsewhere.

The firmware and the app are a separate matter, and far more open. They live on
GitHub under their own licence, and you are free to read them, change them and
build on them.

***

## The print profile

Its own name and description field, separate from everything above. The name
is already right: "0.2mm layer, 2 walls, 30% infill".

The description box is a small editor with bullets but no headings. Paste this
as a bulleted list. The third line is the only one that can ruin a print, so it
is not buried at the bottom.

> - 0.2 mm layer height, 2 walls, 5 top and 3 bottom, 30% grid infill
> - Three plates: the front on edge, the back flat, and four screen spacers
> - Tree supports are painted on the front only, and are already in the file
> - Leave supports OFF for the back. The stand prints in place in a shallow
>   recess, and support material in that gap welds it shut
> - Printed in Bambu PLA Basic, Pumpkin Orange (10301), on a P1S with a 0.4
>   nozzle. Nothing here is printer-specific
> - Every part fits an A1 mini bed: the tallest is the front at 88 mm standing
>   on a 19 x 120 mm footprint

IF THE PAUSE STAYS IN THE UPLOADED 3MF, a sixth line has to go in, naming what
to drop in at 1.6 mm. A printer stopping with nothing on the page to explain it
reads as a fault. If the pause is stripped, say nothing.

Printer compatibility: leaving every box ticked is right. Checked the geometry
against the smallest bed Bambu sells (A1 mini, 180 x 180 x 180) and all six
parts fit with room to spare.

***

## The community post

Enabling Community Post opens its own text box, capped at **500 characters** -
not words. It is a feed post, not the description: two paragraphs, the first
naming the problem, the second the build. 445 characters, so there is room to
adjust a line without a rewrite.

> If you use Claude Code or Codex you have a 5-hour limit and a 7-day one, and
> no way to see where you stand without stopping to check. BLINK shows both on
> your desk: two dials, green to amber to red, counting down to each reset. It
> reads what the tools already write on your own machine, so nothing leaves it.
>
> One $12 ESP32 board and six printed parts. No screws, nothing to solder, and
> a stand that prints in place and folds flat into the back.

What had to go, and why, in case the cap ever moves: the session pips, the
sleep behaviour, the two provider pages, the supports warning, and the "post a
make" invitation. All of them are in the description, which has no such cap.
Markdown is left out entirely - 500 characters is too few to spend on asterisks
that may render literally.

***

<!--
BEFORE PUBLISHING

  [ ] decide what to say about the PAUSE (below)
  [ ] pick the MakerWorld licence (below)
  [ ] fill the Bill of Materials field with the board and the cable, with links
  [ ] check the assembly steps: the 3MF says what the parts ARE, not the order
      they go together in, so those three lines are inference from the geometry
  [ ] optional: open with why you built it. Two of the three comparables do,
      and it is the part nobody can copy. Left out rather than invented.
  [ ] fill the Bill of Materials: CYD board x1, USB data cable x1 under "List
      other parts"; Bambu PLA Basic / Pumpkin Orange (10301) / Filament with
      spool under Filaments. The buy list says "Links are in the Bill of
      Materials", which is false until that field exists
  [ ] optional: the 3MF's filament swatch is #FF6A13 (Bambu Orange 10300), but
      the units were actually printed in Pumpkin Orange 10301 (#FF9016), so
      the slicer colour was never updated when the spool changed. Cosmetic
      only - the PLA Basic profile prints the same either way - but someone
      opening the project sees a colour the BOM contradicts
  [ ] add the model's URL to README.md, in the Open source section

No print TIME here on purpose: the project is unsliced, so the file carries
none, and MakerWorld works its own out from the upload anyway.

THE PAUSE AT 1.6 mm

Metadata/custom_gcode_per_layer.xml in blink_claude_edition.3mf puts `M400 U1`
- a pause - at top_z 1.6 on more than one plate. A printer that stops on its
own, with nothing on the page to explain it, reads as a fault.

Our own units have an NFC tag under the shell (step 02 on the boxed card), and
1.6 mm is about where you would drop one in, so that is the likely reason. A
maker has no tag to insert. Either strip the pause from the uploaded 3MF, or
keep it and say on the page what it is for. Do not leave it silent.

THE LICENCE: STANDARD DIGITAL FILE LICENSE (decided 2026-09-12)

Kfir's call, made looking at the form: no commercial use, and nothing changed.
That is the Standard Digital File License, the strictest MakerWorld offers -
"You shall not share, sub-license, sell, rent, host, transfer, or distribute in
any way the digital or 3D printed versions of this object, nor any other
derivative work... The objects may not be used without permission in any way
whatsoever in which you charge money, or collect fees."

Set the four radios to No / No / No / No. The badge under the box should read
Standard Digital File License; it is the default, so nothing needs changing.

The description's licence section was rewritten to match. It previously said
"free to change, free to give away", which this licence forbids, and a listing
whose prose contradicts its own badge is worse than either alone.

TWO THINGS THAT FOLLOW FROM IT

  - A LICENCE BINDS LICENSEES, NOT THE OWNER. Kfir holds the copyright, so
    SDFL on MakerWorld does not stop him publishing the same STLs on GitHub,
    blink-buddy.com, or anywhere else, under any terms he likes. Nothing here
    is a lock-in for him; it only binds people who download from MakerWorld.

  - IT DISAGREES WITH README.md. The repo's Open source section says "Build
    one. Build ten... Give them to your friends." Under SDFL a person may not
    hand on a printed case. The software and the model are separately licensed
    and may legitimately differ, but if anyone ever asks, that is the answer,
    and it is worth saying out loud rather than discovering.

WHAT WAS CONSIDERED INSTEAD

Read off three real CYD listings on 2026-09-11. The choice is wider than
Creative Commons, and every option already forbids selling - so the platform,
not our paragraph, is what actually enforces it.

  - Standard Digital File License (HandyHack, 1940770): "You shall not share,
    sub-license, sell, rent, host, transfer, or distribute in any way the
    digital or 3D printed versions of this object, nor any other derivative
    work." Forbids selling AND all redistribution. Too strict for us: it
    contradicts "free to give away".

  - MakerWorld Exclusive License (CYD Desk Buddy, 2787810): derivatives are
    allowed but must be published EXCLUSIVELY on MakerWorld, and commercial use
    is "strictly prohibited". The no-selling half is right; the exclusivity
    half locks remixes to one platform, which is not our bargain to impose.

  - Creative Commons BY-NC-ND (Case for ESP32 CYD, 1033712): no derivatives at
    all, so it contradicts "free to change".

CC BY-NC-SA 4.0 remains the pick: print it, change it, re-share it, credit
BLINK, never sell it, and pass the same terms on. It is the only one of the
four that says what the description says.

This governs the MODEL FILES. The software keeps its own licence in LICENSE at
the root of the repo. The two need not match; they only have to agree about
selling, and all of these do.

WHERE THE SHAPE OF THIS PAGE CAME FROM

  - CYD Desk Buddy (2787810) is the closest comparable and the best written:
    what it is, why I made it, Features, Hardware, Printing, Assembly/Wiring,
    Notes. Plain headings, no emoji. This page follows it.
  - HandyHack (1940770) uses emoji headings and a longer feature list. Louder,
    and it works for a bristling gadget; wrong register for this one.
  - Case for ESP32 CYD (1033712) is four short paragraphs and a GitHub link -
    about as thin as a listing gets, and it still repeats its print settings.

  All three repeat print settings in the description even though the profile
  carries them, and all three name the exact board. Two of the three open with
  why the maker built it before saying what it is.

  There is a structured BILL OF MATERIALS field, separate from the description,
  that takes purchase links. "Links are in the Bill of Materials" above is the
  same line the best comparable uses, and it is a lie until that field is
  filled.

A SHORTER VERSION EXISTS

The 285-word cut is in the scratchpad as makerworld-minimal.md. It reads well
but it is thinner than anything on the platform, and it drops the two things
every comparable keeps: the print settings and the exact board behaviour.
-->
