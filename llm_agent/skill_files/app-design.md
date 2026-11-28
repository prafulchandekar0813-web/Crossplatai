---
name: app-design
description: The design process for any app with screens (reference, brief, build, review), what a good screen has, and the Android and Windows layouts.
---
DESIGN PROCESS (every app with screens; a plain page is not acceptable): 

1) REFERENCE: before writing any UI, find a real open-source project of this kind: call use_skill("github-ui") and follow it (github_search, then github_clone the best one). A copied project becomes the app: keep its framework and structure, change it in place, and put the brief into the Plan section of its PROJECT.md. Also look at 1 or 2 good-looking real apps with research_web or read_url. Note their screens, navigation, spacing, colours and the features users expect. 

2) BRIEF: write design_brief.md in the project folder: who it is for, the screens, the navigation for each platform, a palette with hex codes, fonts, the signature feature that makes this app different, and the sources you studied. 

3) BUILD from the brief and make it your own: a new name, palette, icon set, copy and layout details, never the reference's name, logo, pictures or text. You may reuse small parts of code only from projects with a permissive licence (MIT, Apache-2.0, BSD, ISC): keep the licence notice and credit them in an About screen. Never copy GPL, AGPL or unlicensed code; use those only as inspiration. 

4) REVIEW: run check_build (pass platforms), and treat every line of 'design' as work to do before you finish. 

A good screen has a clear hierarchy (a headline, a title, body and small text), a small consistent palette in CSS variables, cards and spacing, icons (inline SVG), real empty and loading states, hover and pressed states, a dark theme, and a signature touch. A screen of plain headings and a list is a failure: add a hero or summary at the top, grouped content, search or filters where lists are long, and quick actions. 

PLATFORMS: the user may ask for Android, Windows or both, and the two should not look the same. 

Android: Material-style top app bar, bottom navigation with 3 to 5 tabs, one main floating action button, cards, bottom sheets for details and choices, pull to refresh, search, 48px touch targets, thumb-reachable actions, respect the back gesture, dark mode. 

Windows: a side navigation rail or sidebar, a command bar on top, a list on the left with the detail beside it, multi-column content using the width, hover and keyboard focus states, right-click or menu buttons for extra actions, keyboard shortcuts, resizable windows. 

For both: one page with two layouts from the same data: bottom tabs below about 900px wide and a side rail above it. The Android and Windows apps add WorkbenchShell/android or WorkbenchShell/windows to navigator.userAgent, so a small script can set document.documentElement.dataset.platform for platform-only touches.
