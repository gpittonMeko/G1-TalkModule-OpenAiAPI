# G1 Talk - APK Android

App Android che funziona come launcher per connettersi al G1 Talk Module (robot o AI Accelerator).

## Requisiti

- Node.js 18+
- Android Studio (per build APK)
- Java 17

## Antivirus (Avast / AVG)

Gradle può essere segnalato come **IDP.Generic** sul file `gradle-launcher-*.jar`: è un **falso positivo**. Aggiungi un’eccezione per `C:\Users\<tu>\.gradle\` e ripeti la build.

## SDK senza Amministratore

Se Gradle segnala **licenze non accettate** sul SDK in `Program Files`, dalla root del repo:

```powershell
.\scripts\setup_user_android_sdk.ps1
.\capacitor-app\build_apk.ps1
```

Creiamo `%USERPROFILE%\AndroidSdkG1` con platform 32 e aggiorniamo `android\local.properties`.

## Build APK

```bash
cd capacitor-app
npm init -y
npm install @capacitor/core @capacitor/cli @capacitor/android
npx cap add android
npx cap sync
npx cap open android
```

In Android Studio: Build → Build Bundle(s) / APK(s) → Build APK(s).

L'APK sarà in `android/app/build/outputs/apk/debug/`.

## Cassa Bluetooth

L'APK apre la stessa interfaccia web del server: STT/LLM/TTS restano sul **G1** (o PC server). Per riprodurre l'audio su un altoparlante Bluetooth: accoppia il telefono alla cassa in **Impostazioni → Bluetooth**; Android invia l'audio multimediale (browser/WebView) all'uscita selezionata.

## Uso

1. Avvia il server sul G1: `bash scripts/restart_server.sh` e annota l'IP
2. Installa l'APK sul telefono
3. Collega il telefono al WiFi del robot (es. Unitree G1) o alla stessa rete dell'AI Accelerator
4. Apri l'app G1 Talk
5. Inserisci l'IP:
   - **Robot G1**: 192.168.123.161 (WiFi diretto al robot)
   - **AI Accelerator**: 192.168.10.191 (stessa rete)
6. Tocca "Connetti"

L'app aprirà l'interfaccia G1 Talk nel browser integrato. Per il microfono serve HTTPS: al primo accesso accetta il certificato (Avanzate → Procedi).
