# Shell build repository

Copy this folder's contents into a NEW private GitHub repository (the `shell/` folder and `.github/`).
The workbench server starts `build-shell.yml` for each app; GitHub builds the APK (Linux) and the Windows EXE and keeps the files for 7 days;
the server downloads them and offers them in Manage app. Needs on the server: `BUILD_GITHUB_TOKEN` (fine-grained token, this repo only,
"Actions: read and write"), and Admin > Limits > build repository set to `owner/repo`.

The APK is signed with Flutter's debug key: fine for installing directly, not accepted by Google Play. The EXE is unsigned (Windows SmartScreen warns).
Not tested in the environment that wrote it: expect to fix the first run.
