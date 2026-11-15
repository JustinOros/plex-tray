# plex-tray

A system tray app that remote controls Plex playback in your default browser and can show what you are watching on Discord.

Runs on Windows, macOS and Linux as a single Python script.

## Features

- Play, pause, previous, next, back 10 seconds, forward 30 seconds and stop from the tray
- Continue Watching, recently added per library, and search
- Playing a show resumes the next episode and keeps going through the season
- Movies and shows play in your default browser using Plex Web
- Optional Discord Rich Presence with a title hiding option
- Sign in with Plex or paste a token, or use no token on a server that allows your network without auth
- Missing Python packages install automatically on first launch

## Quick start

```
python3 plex-tray.py
```

1. Enter your Plex URL when asked, for example `http://192.168.1.10:32400`
2. Open Settings and choose Sign In With Plex if the server asks for a token
3. In Plex Web, turn on Advertise as a player in the Plex Web settings

Step 3 is what lets the tray send play, pause and skip commands to the browser tab. Keep a Plex Web tab open. The first play after opening a fresh tab may need one click inside the tab because of browser autoplay rules.

## Discord

1. Create an application at https://discord.com/developers/applications and name it Plex
2. Copy the Application ID
3. In the tray, open Settings, choose Discord Application ID and paste it
4. Turn on Show On Discord

The Discord desktop app must be running on the same computer. Turn on Hide Title On Discord to show only Watching a movie or Watching a show. For an icon, upload an image named `plex` under Rich Presence, Art Assets in your Discord application.

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
