# Hand Tracking Streamer — Meta Quest 3

## Fonti ufficiali (controllate)

| Cosa | URL |
|------|-----|
| **Codice / release APK v1.1.0** | https://github.com/wengmister/hand-tracking-streamer/releases/tag/v1.1.0 |
| **Download diretto APK** | https://github.com/wengmister/hand-tracking-streamer/releases/download/v1.1.0/hand_tracking_streamer.apk |
| **Meta Quest Store (stessa app)** | https://www.meta.com/experiences/hand-tracking-streamer/26303946202523164 |
| **SideQuest** | https://sidequestvr.com/app/46236/hand-tracking-streamer |

**SHA-256** atteso per `hand_tracking_streamer.apk` (v1.1.0, digest API GitHub):

`e0b41ab36e0bcbbc1a1d616bd659bad6d93c02efcb332c1d87c2bee1c6e04879`

## Sul PC (repo)

- APK locale (non in git): `installers/quest/hand_tracking_streamer.apk`
- Script: dalla root progetto  
  `.\scripts\install_hand_tracking_streamer_quest.ps1`

## Dipendenze PC

- **adb**: `winget install Google.PlatformTools`  
  (lo script prova anche a installarlo se manca)

## Se `adb devices` dice **unauthorized**

1. Indossa il Quest, guarda **dentro** la cuffia.  
2. Accetta **Consenti debug USB** / chiave RSA del PC.  
3. Rilancia lo script.

## Avast / antivirus (IDP.Generic)

Falso positivo frequente su `adb` e APK sideload. Eccezione per `platform-tools` e `installers/quest` se la fonte e solo GitHub sopra.
