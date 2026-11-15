# plex-tray

A system tray app that opens Plex titles in your default browser and can show what you are watching on Discord.

Runs on Windows, macOS and Linux as a single Python script.

## Features

- Continue Watching, recently added per library, and search
- Picking a title opens it in your default browser using Plex Web, ready to play
- Picking a show opens the next episode to watch
- Optional Discord Rich Presence with a title hiding option, driven by your Plex session
- Sign in with Plex or paste a token, or use no token on a server that allows your network without auth
- Menus and dialogs follow the system dark or light theme
- Missing Python packages install automatically on first launch

Plex removed remote control support from Plex Web, so play, pause and skip stay in the browser.

## Quick start

```
python3 plex-tray.py
```

1. Enter your Plex URL when asked, for example `http://192.168.1.10:32400`
2. Open Settings and choose Sign In With Plex if the server asks for a token
3. Pick a title from the tray menu and press play in the browser tab

## Discord

1. Create an application at https://discord.com/developers/applications and name it Plex
2. Copy the Application ID
3. In the tray, open Discord Presence, choose Application ID and paste it
4. Turn on Show What I am Watching

The Discord desktop app must be running on the same computer. Turn on Hide Title to show only Watching a movie or Watching a show. For an icon, upload an image named `plex` under Rich Presence, Art Assets in your Discord application.

Only your own Plex session is shown. If the app cannot tell which user you are, it only reports when exactly one session exists on the server.

## Linux

Install tkinter and the tray backend first:

```
sudo apt install python3-tk python3-gi gir1.2-ayatanaappindicator3-0.1
```

## Stop and restart

```
python3 plex-tray.py --stop
python3 plex-tray.py --restart
```

## Files

Settings and logs live here:

- Windows: `%APPDATA%\plex-tray`
- macOS: `~/Library/Application Support/plex-tray`
- Linux: `~/.config/plex-tray`

Set `PLEX_TRAY_HOME` to use a different folder. `state.json` holds your Plex token, so keep it private.
