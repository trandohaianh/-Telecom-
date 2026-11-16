# Build app Android, bật máy ảo Pixel, cài và mở app "Lá chắn".
# Yêu cầu: bộ công cụ ở D:\AndroidDev (JDK 17 + Android SDK) và server đang chạy: python run_demo.py
# Chạy:  powershell -ExecutionPolicy Bypass -File v2\android\run_android.ps1
param(
    [string]$Root = "D:\AndroidDev",
    [string]$Avd = "LaChan_Pixel"
)
$ErrorActionPreference = "Stop"
$env:JAVA_HOME = "$Root\jdk17"
$env:ANDROID_HOME = "$Root\sdk"
$env:ANDROID_SDK_ROOT = "$Root\sdk"
$env:ANDROID_USER_HOME = "$Root\user"        # cấu hình + máy ảo nằm ở ổ D, không đụng ổ C
$env:ANDROID_AVD_HOME = "$Root\user\avd"
$env:GRADLE_USER_HOME = "$Root\gradle-home"
$adb = "$Root\sdk\platform-tools\adb.exe"
$emu = "$Root\sdk\emulator\emulator.exe"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# 1. Tạo máy ảo nếu chưa có (Pixel 6, Android 14, x86_64).
if (-not (Test-Path "$env:ANDROID_AVD_HOME\$Avd.avd")) {
    Write-Host "Tạo máy ảo $Avd ..."
    "no" | & "$Root\sdk\cmdline-tools\latest\bin\avdmanager.bat" create avd -n $Avd -k "system-images;android-34;google_apis;x86_64" -d pixel_6 --force
}

# 2. Build APK debug.
Write-Host "Build APK ..."
Set-Content -Path "$here\local.properties" -Value ("sdk.dir=" + ($env:ANDROID_HOME -replace '\\', '\\'))
Push-Location $here
& .\gradlew.bat assembleDebug --console=plain
Pop-Location
$apk = "$here\app\build\outputs\apk\debug\app-debug.apk"

# 3. Bật máy ảo nếu chưa chạy, chờ khởi động xong.
if (-not ((& $adb devices) -match "emulator-")) {
    Write-Host "Bật máy ảo ..."
    # swiftshader_indirect = dựng hình bằng phần mềm: "-gpu auto" làm máy ảo crash với driver NVIDIA GTX 960M của máy này.
    Start-Process -FilePath $emu -ArgumentList @("-avd", $Avd, "-no-snapshot-save", "-gpu", "swiftshader_indirect", "-no-boot-anim")
}
& $adb wait-for-device
do { Start-Sleep -Seconds 2; $boot = (& $adb shell getprop sys.boot_completed 2>$null) } until ($boot -match "1")

# 4. Cài và mở app.
& $adb install -r $apk
& $adb shell am start -n vn.lachan.fraudshield/.MainActivity
Write-Host "Xong. App 'Lá chắn' đang mở trên máy ảo (server: http://10.0.2.2:8080/app/)."
