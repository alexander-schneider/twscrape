# Browser-recorded animation-key vectors

`vectors.json` and `live_page.html` are copied unchanged from
[anatolykoptev/go-twitter](https://github.com/anatolykoptev/go-twitter) at commit
`14a82e60b4cce82cbe7219e23d96464307c338c9` (`xtid/testdata/browser/vectors.json` and
`xtid/testdata/reference/live_page.html`), licensed under the Apache License 2.0.

Each vector pairs a 48-byte verification key with the animation key that X's own
script computed for it in headless Chromium 156 (generated 2026-11-03). The
classes include colour channels above 255 or below 0 before the clamp and curves
where plain bisection picks a different easing value. The key-byte indices
`[14, 20, 19, 30]` are the ones the recorded `ondemand.s` script carries.
