# KevenOS hall display

Run `npm ci --ignore-scripts` and `npm run build` in this folder after changing the Remotion Player. The generated browser bundle is `../static/live-player.js`; the Python image packages it with the existing static files. The server publishes the bundle and the matching stylesheet on the existing asset allowlist.

The player receives only the existing audience projection. The Scoring Desk controls the current programme, clock and reveal state. Use the viewer link from the Desk to open the landscape display. The hall leaderboard shows up to five rows per page. The existing `rotate` query option can rotate the remaining pages. Portrait screens retain the compact layout. On a disconnect the hall display freezes the last visible clock and shows a reconnect status. Loss of viewing authorization clears the stage.
