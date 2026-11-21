#!/usr/bin/env bash
set -Eeuo pipefail

fail() { printf '%s\n' "$*" >&2; exit 1; }

# Bound length before shell arithmetic to prevent overflow, octal or expression inputs.
[[ $# == 2 && $1 =~ ^[1-9][0-9]{0,9}$ && $2 =~ ^[1-9][0-9]{0,9}$ ]] ||
    fail 'Expected two canonical positive Android versionCodes.'
(( $2 <= 2100000000 && $2 == $1 + 1 )) ||
    fail 'Expected two consecutive Android versionCodes at or below 2100000000.'
versions=("$1" "$2")
retain_bundles=${RETAIN_BUNDLES:-false}
[[ $retain_bundles == true || $retain_bundles == false ]] || fail 'Expected a boolean retain_bundles input.'
[[ ${GITHUB_SHA:-} =~ ^[0-9a-f]{40}$ && $(git rev-parse HEAD) == "$GITHUB_SHA" ]] ||
    fail 'Checkout SHA differs from the workflow SHA.'

package=io.github.linuxerlv.tidelinepilot
export JAVA_HOME="${JAVA_HOME_17_X64:?Expected the preinstalled JDK 17.}"
export PATH="$JAVA_HOME/bin:$PATH"
sdk=${ANDROID_HOME:?Expected the preinstalled Android SDK.}
tools="$sdk/build-tools/36.0.0"
sdkmanager="$sdk/cmdline-tools/latest/bin/sdkmanager"
avdmanager="$sdk/cmdline-tools/latest/bin/avdmanager"
adb="$sdk/platform-tools/adb"
emulator="$sdk/emulator/emulator"
image='system-images;android-36;google_apis;x86_64'
java_version=$(javac -version)
[[ $java_version == 'javac 17.'* && -r $sdk/platforms/android-36/android.jar &&
   -x $tools/aapt2 && -x $tools/d8 && -x $tools/apksigner && -x $sdkmanager && -x $avdmanager ]] ||
    fail 'Expected JDK 17 and Android SDK platform 36 and build tools 36.0.0.'
[[ -c /dev/kvm ]] || fail 'The hosted runner has no KVM device.'
if [[ ! -r /dev/kvm || ! -w /dev/kvm ]]; then
    sudo chown "$(id -u)" /dev/kvm
fi

umask 077
scratch=$(mktemp -d "${RUNNER_TEMP:?Expected the runner scratch directory.}/tideline-android.XXXXXXXX")
emulator_pid=''
adb_started=false
report=''
device() { timeout 45s "$adb" -s emulator-5554 "$@"; }
cleanup() {
    local status=$? attempt
    trap - EXIT
    if [[ -n $emulator_pid ]]; then
        timeout 8s "$adb" -s emulator-5554 emu kill >/dev/null 2>&1 || true
        kill -TERM -- "-$emulator_pid" 2>/dev/null || true
        for attempt in {1..5}; do
            kill -0 -- "-$emulator_pid" 2>/dev/null || break
            sleep 1
        done
        kill -KILL -- "-$emulator_pid" 2>/dev/null || true
        wait "$emulator_pid" 2>/dev/null || true
        if (( status != 0 )); then tail -n 80 "$scratch/emulator.log" >&2; fi
    fi
    if "$adb_started"; then timeout 8s "$adb" kill-server || status=1; fi
    timeout 10s rm -rf -- "$scratch" || status=1
    if (( status == 0 )) && [[ -n $report ]]; then printf 'KPOOL_ANDROID_PILOT_OK %s\n' "$report"; fi
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

jar="$scratch/bundletool.jar"
curl --fail --silent --show-error --location --retry 2 --max-time 120 \
    --proto '=https' --proto-redir '=https' --output "$jar" \
    'https://github.com/google/bundletool/releases/download/1.18.2/bundletool-all-1.18.2.jar'
printf '378b5434cd1378bef6b2bc527b8c7f0ff2584b273830335bce54d6d0813c8584  %s\n' "$jar" | sha256sum --check
bundletool() { java -jar "$jar" "$@"; }

keystore="$scratch/fixture.p12"
password="$scratch/password"
alias=tideline-pilot
openssl rand -hex 24 > "$password"
# jarsigner strict verification requires an issued signer. Trust the disposable root in this store only.
openssl req -x509 -newkey rsa:2048 -noenc -sha256 -days 1 -subj '/CN=Tideline Android pilot root' \
    -keyout "$scratch/root.key" -out "$scratch/root.pem" \
    -addext 'basicConstraints=critical,CA:TRUE,pathlen:0' -addext 'keyUsage=critical,keyCertSign'
openssl req -new -newkey rsa:2048 -noenc -subj '/CN=Tideline Android pilot fixture' \
    -keyout "$scratch/fixture.key" -out "$scratch/fixture.csr"
printf '%s\n' 'basicConstraints=critical,CA:FALSE' 'keyUsage=critical,digitalSignature' \
    'extendedKeyUsage=codeSigning' > "$scratch/fixture.ext"
openssl x509 -req -in "$scratch/fixture.csr" -CA "$scratch/root.pem" -CAkey "$scratch/root.key" \
    -CAcreateserial -sha256 -days 1 -extfile "$scratch/fixture.ext" -out "$scratch/fixture.pem"
openssl pkcs12 -export -inkey "$scratch/fixture.key" -in "$scratch/fixture.pem" -certfile "$scratch/root.pem" \
    -name "$alias" -passout "file:$password" -out "$keystore"
keytool -importcert -noprompt -keystore "$keystore" -storetype PKCS12 -storepass:file "$password" \
    -alias tideline-pilot-root -file "$scratch/root.pem"
openssl x509 -in "$scratch/fixture.pem" -outform DER -out "$scratch/fixture.der"
certificate_sha256=$(sha256sum "$scratch/fixture.der" | cut -d ' ' -f1)

bundle_sha256=''
for version in "${versions[@]}"; do
    aab="$scratch/$version.aab"
    apk="$scratch/$version.apk"
    apks="$scratch/$version.apks"
    python scripts/build_android.py --version-code "$version" --bundletool "$jar" --out "$aab"
    jarsigner -keystore "$keystore" -storetype PKCS12 -storepass:file "$password" \
        -sigalg SHA256withRSA -digestalg SHA-256 "$aab" "$alias"
    jarsigner -verify -strict -keystore "$keystore" -storetype PKCS12 -storepass:file "$password" "$aab" "$alias"
    bundletool validate --bundle="$aab"
    bundletool dump manifest --bundle="$aab" > "$scratch/manifest.xml"

    actual_bundle_sha=$(python - "$aab" "$scratch/manifest.xml" "$GITHUB_SHA" "$version" "$package" <<'PY'
from hashlib import sha256
import json
import sys
from xml.etree import ElementTree
from zipfile import ZipFile

bundle, manifest_path, source_sha, version, package = sys.argv[1:]
android = "{http://schemas.android.com/apk/res/android}"
manifest = ElementTree.parse(manifest_path).getroot()
sdk = manifest.find("uses-sdk")
if (manifest.tag != "manifest" or manifest.get("package") != package
        or manifest.get(android + "versionCode") != version
        or manifest.get(android + "versionName") != f"1.0.{version}"
        or sdk is None or sdk.get(android + "targetSdkVersion") != "36"):
    raise SystemExit("Android bundle manifest has an unexpected package, version, or target SDK.")
if any(node.get(android + "name") == "android.permission.INTERNET" for node in manifest):
    raise SystemExit("The offline Android bundle must not request internet permission.")
with ZipFile(bundle) as archive:
    html_path = "base/assets/tideline.html"
    metadata_path = "base/assets/tideline-build.json"
    for name in (html_path, metadata_path):
        if archive.namelist().count(name) != 1:
            raise SystemExit(f"Expected exactly one Android bundle entry {name}.")
    html = archive.read(html_path)
    metadata = json.loads(archive.read(metadata_path))
    digest = sha256(html).hexdigest()
    if not html or type(metadata.get("version_code")) is not int or metadata != {
        "source_sha": source_sha, "bundle_sha256": digest, "version_code": int(version), "package": package,
    }:
        raise SystemExit("Embedded Android metadata differs from the signed bundle or checkout.")
    print(digest)
PY
)
    [[ -z $bundle_sha256 || $bundle_sha256 == "$actual_bundle_sha" ]] ||
        fail 'The two version fixtures contain different Tideline HTML.'
    bundle_sha256=$actual_bundle_sha
    bundletool build-apks --bundle="$aab" --output="$apks" --mode=universal \
        --ks="$keystore" --ks-key-alias="$alias" --ks-pass="file:$password" --key-pass="file:$password"
    unzip -p "$apks" universal.apk > "$apk"
done

export ANDROID_AVD_HOME="$scratch/avd"
export ANDROID_USER_HOME="$scratch/android-user"
mkdir -p "$ANDROID_AVD_HOME" "$ANDROID_USER_HOME"
needed=()
[[ -x $adb ]] || needed+=(platform-tools)
[[ -x $emulator ]] || needed+=(emulator)
[[ -r $sdk/system-images/android-36/google_apis/x86_64/package.xml ]] || needed+=("$image")
if (( ${#needed[@]} )); then timeout 8m "$sdkmanager" --install "${needed[@]}" </dev/null; fi
"$emulator" -accel-check
printf 'no\n' | "$avdmanager" create avd --name tideline-pilot --package "$image" --abi x86_64
adb_started=true
timeout 20s "$adb" start-server
# Keep GLES available for WebView and disable Vulkan after the hosted emulator crashed during upgrade.
setsid "$emulator" -avd tideline-pilot -port 5554 -accel on -no-window -gpu swiftshader \
    -feature -Vulkan -no-audio -no-boot-anim -no-snapshot -no-metrics > "$scratch/emulator.log" 2>&1 &
emulator_pid=$!
boot_deadline=$((SECONDS + 300))
until [[ $(device shell getprop sys.boot_completed 2>/dev/null) == '1' ]]; do
    kill -0 "$emulator_pid" 2>/dev/null && (( SECONDS < boot_deadline )) || fail 'The Android emulator failed to boot.'
    sleep 2
done
[[ $(device shell getprop ro.build.version.sdk) == '36' && $(device shell getprop ro.product.cpu.abi) == 'x86_64' ]] ||
    fail 'The booted emulator does not report Android API 36 x86_64.'
device shell input keyevent 82
device shell cmd connectivity airplane-mode enable
device shell svc wifi disable
device shell svc data disable
offline() {
    local connectivity
    [[ $(device shell settings get global airplane_mode_on) == '1' ]] || return 1
    connectivity=$(device shell dumpsys connectivity) || return 1
    grep -Exq '[[:space:]]*Active default network: none' <<< "$connectivity"
}
offline_deadline=$((SECONDS + 30))
until offline; do
    (( SECONDS < offline_deadline )) || fail 'The emulator did not become offline.'
    sleep 2
done

installed_package() { device shell cmd package list packages --show-versioncode "$package"; }
installed=$(installed_package)
[[ -z $installed ]] || fail 'The fresh emulator already contains the pilot package.'
install_flags=()
for version in "${versions[@]}"; do
    printf 'Android pilot installing versionCode %s.\n' "$version"
    if ! install_result=$(device install "${install_flags[@]}" "$scratch/$version.apk" 2>&1) ||
        ! grep -Fxq 'Success' <<< "$install_result"; then
        printf '%s\n' "$install_result" >&2
        fail "Failed to install version $version."
    fi
    install_flags=(-r)
    installed=$(installed_package)
    [[ $installed == "package:$package versionCode:$version" ]] || fail "Wrong installed version for $package."
    installed_version=${installed##*:}
    apk_path=$(device shell pm path "$package")
    [[ $apk_path =~ ^package:/data/app/[^[:space:]]+/base\.apk$ ]] || fail 'Expected one installed base APK.'
    device pull "${apk_path#package:}" "$scratch/installed.apk"
    cmp -s "$scratch/$version.apk" "$scratch/installed.apk" || fail 'Installed APK differs from the bundletool APK.'
    signature=$("$tools/apksigner" verify --print-certs "$scratch/installed.apk")
    installed_certificate=$(awk -F ': ' '/^Signer #[0-9]+ certificate SHA-256 digest: / {print $2}' <<< "$signature")
    [[ $installed_certificate == "$certificate_sha256" ]] || fail 'Installed APK has the wrong signing certificate.'

    offline || fail 'The emulator became connected before launch.'
    device shell am force-stop "$package"
    device logcat -c
    start_result=$(device shell am start -W -n "$package/.MainActivity")
    grep -Fxq 'Status: ok' <<< "$start_result" || fail "MainActivity did not start for version $version."
    pid=$(device shell pidof -s "$package")
    [[ $pid =~ ^[0-9]+$ ]] || fail 'MainActivity did not start a process.'
    expected_ready="READY source=$GITHUB_SHA version=$version cells=49"
    ready_deadline=$((SECONDS + 60))
    while true; do
        webview_log=$(device logcat -d -v raw --pid="$pid" 'TidelinePilot:I' '*:S')
        if grep -Fxq "$expected_ready" <<< "$webview_log"; then break; fi
        (( SECONDS < ready_deadline )) || fail "Tideline WebView did not report readiness for version $version."
        sleep 1
    done
    offline || fail 'The emulator became connected during the WebView check.'
    aab_sha256=$(sha256sum "$scratch/$version.aab" | cut -d ' ' -f1)
    jq -cn --argjson version "$version" --arg aab "$aab_sha256" --argjson installed "$installed_version" \
        '{version_code: $version, aab_sha256: $aab, installed_version_code: $installed, webview_cells: 49}' \
        >> "$scratch/builds.jsonl"
    printf 'Android pilot verified versionCode %s offline with 49 WebView cells.\n' "$version"
done

report=$(jq -cs --arg source_sha "$GITHUB_SHA" --arg package "$package" --arg bundle_sha256 "$bundle_sha256" \
    --arg certificate_sha256 "$certificate_sha256" \
    '{source_sha: $source_sha, package: $package, bundle_sha256: $bundle_sha256, certificate_sha256: $certificate_sha256, builds: .}' \
    "$scratch/builds.jsonl")

if [[ $retain_bundles == true ]]; then
    export_dir="$RUNNER_TEMP/tideline-android-aabs"
    mkdir -- "$export_dir"
    cp -- "$scratch/${versions[0]}.aab" "$scratch/${versions[1]}.aab" "$export_dir/"
fi
