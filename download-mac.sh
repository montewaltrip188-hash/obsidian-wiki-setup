#!/bin/sh
set -eu

repository='montewaltrip188-hash/obsidian-wiki-setup'
stable_url="https://raw.githubusercontent.com/${repository}/main/release/stable.json"
expected_key_id='c1f596094a9a54ada888502a2ab7ef6bc5fecf82d4281dd4bbae2ae7bc9d9938'
expected_pem_sha256='a350fcd7160d8ca6a06d73da95061ae026424eed5b4cfb13dd21eec8cd465d3b'
destination_root="${1:-$HOME/Downloads}"
temp_root="${TMPDIR:-/tmp}"
work="$(mktemp -d "${temp_root%/}/obsidian-wiki-download.XXXXXX")"

cleanup() {
  case "$work" in
    "${temp_root%/}"/*) rm -rf -- "$work" ;;
    *) printf '%s\n' '拒绝清理临时目录之外的路径' >&2; exit 2 ;;
  esac
}
trap cleanup EXIT HUP INT TERM

download() {
  case "$1" in
    https://github.com/*|https://raw.githubusercontent.com/*) ;;
    *) printf '拒绝非 GitHub HTTPS 下载地址：%s\n' "$1" >&2; exit 2 ;;
  esac
  curl --fail --location --silent --show-error --proto '=https' --tlsv1.2 "$1" -o "$2"
}

json_get() {
  /usr/bin/plutil -extract "$2" raw -o - "$1"
}

sha256_file() {
  /usr/bin/shasum -a 256 "$1" | awk '{print $1}'
}

assert_file() {
  actual_sha="$(sha256_file "$1")"
  actual_size="$(/usr/bin/stat -f '%z' "$1")"
  [ "$actual_sha" = "$2" ] && [ "$actual_size" = "$3" ] || {
    printf '%s 的长度或 SHA-256 不匹配\n' "$4" >&2
    exit 2
  }
}

stable="$work/stable.json"
download "$stable_url" "$stable"
pointer_format="$(json_get "$stable" pointer_format)"
channel="$(json_get "$stable" channel)"
release_state="$(json_get "$stable" release_state)"
pointer_repository="$(json_get "$stable" repository)"
version="$(json_get "$stable" bundle_version)"
tag="$(json_get "$stable" tag)"
key_id="$(json_get "$stable" trust.key_id)"
pem_sha="$(json_get "$stable" trust.pem.sha256)"
[ "$pointer_format" = 1 ] && [ "$channel" = stable ] && [ "$release_state" = stable ] \
  && [ "$pointer_repository" = "$repository" ] && [ "$tag" = "v$version" ] \
  && [ "$key_id" = "$expected_key_id" ] && [ "$pem_sha" = "$expected_pem_sha256" ] || {
  printf '%s\n' 'stable.json 合同或固定信任根不匹配' >&2
  exit 2
}

manifest_name="$(json_get "$stable" manifest.name)"
manifest_url="$(json_get "$stable" manifest.url)"
manifest_sha="$(json_get "$stable" manifest.sha256)"
manifest_size="$(json_get "$stable" manifest.size)"
signature_name="$(json_get "$stable" signature.name)"
signature_url="$(json_get "$stable" signature.url)"
signature_sha="$(json_get "$stable" signature.sha256)"
signature_size="$(json_get "$stable" signature.size)"
asset_name="$(json_get "$stable" assets.macos-universal.name)"
asset_url="$(json_get "$stable" assets.macos-universal.url)"
asset_sha="$(json_get "$stable" assets.macos-universal.sha256)"
asset_size="$(json_get "$stable" assets.macos-universal.size)"
public_key_url="$(json_get "$stable" trust.pem.url)"
public_key_size="$(json_get "$stable" trust.pem.size)"
release_base="https://github.com/${repository}/releases/download/${tag}"
raw_key_url="https://raw.githubusercontent.com/${repository}/${tag}/release/release-signing-public-key.pem"
[ "$manifest_url" = "$release_base/$manifest_name" ] \
  && [ "$signature_url" = "$release_base/$signature_name" ] \
  && [ "$asset_url" = "$release_base/$asset_name" ] \
  && [ "$public_key_url" = "$raw_key_url" ] || {
  printf '%s\n' 'stable.json 含非预期不可变下载地址' >&2
  exit 2
}

manifest="$work/release-manifest.json"
signature="$work/release-manifest.sig"
public_key="$work/release-signing-public-key.pem"
asset="$work/$asset_name"
download "$manifest_url" "$manifest"
download "$signature_url" "$signature"
download "$public_key_url" "$public_key"
download "$asset_url" "$asset"
assert_file "$manifest" "$manifest_sha" "$manifest_size" 'release manifest'
assert_file "$signature" "$signature_sha" "$signature_size" 'release signature'
assert_file "$public_key" "$expected_pem_sha256" "$public_key_size" 'release public key'
assert_file "$asset" "$asset_sha" "$asset_size" 'macOS 安装资产'

[ "$(json_get "$manifest" release_state)" = stable ] \
  && [ "$(json_get "$manifest" bundle_version)" = "$version" ] \
  && [ "$(json_get "$manifest" required_signature.algorithm)" = 'RSA-SHA256-PKCS1-v1_5' ] \
  && [ "$(json_get "$manifest" required_signature.key_id)" = "$expected_key_id" ] || {
  printf '%s\n' 'release manifest 的稳定版本或签名合同不匹配' >&2
  exit 2
}

found=0
i=0
while [ "$i" -lt 32 ]; do
  path="$(/usr/bin/plutil -extract "files.$i.path" raw -o - "$manifest" 2>/dev/null || true)"
  if [ "$path" = "assets/$asset_name" ]; then
    [ "$(json_get "$manifest" "files.$i.sha256")" = "$asset_sha" ] \
      && [ "$(json_get "$manifest" "files.$i.size")" = "$asset_size" ] || {
      printf '%s\n' 'macOS 安装资产的签名记录不匹配' >&2
      exit 2
    }
    found=$((found + 1))
  fi
  i=$((i + 1))
done
[ "$found" -eq 1 ] || { printf '%s\n' 'macOS 安装资产未被签名 manifest 唯一绑定' >&2; exit 2; }

derived_key_id="$(openssl pkey -pubin -in "$public_key" -outform DER 2>/dev/null | /usr/bin/shasum -a 256 | awk '{print $1}')"
[ "$derived_key_id" = "$expected_key_id" ] || { printf '%s\n' '公钥指纹不匹配' >&2; exit 2; }
openssl dgst -sha256 -verify "$public_key" -signature "$signature" "$manifest" >/dev/null 2>&1 \
  || { printf '%s\n' 'release manifest 的 RSA 签名无效' >&2; exit 2; }

mkdir -p "$destination_root"
destination="${destination_root%/}/Obsidian-LLM-Wiki-$version"
[ ! -e "$destination" ] || { printf '目标已存在，拒绝覆盖：%s\n' "$destination" >&2; exit 2; }
staging="${destination}.staging.$$"
[ ! -e "$staging" ] || { printf 'staging 已存在，拒绝覆盖：%s\n' "$staging" >&2; exit 2; }
mkdir "$staging"
unzip -q "$asset" -d "$staging"
[ -f "$staging/setup-mac.sh" ] || { printf '%s\n' '解压后的安装入口缺失' >&2; exit 2; }
mv "$staging" "$destination"

printf '下载、SHA-256 和 RSA 验签完成：%s\n' "$destination"
printf '下一步：bash %s/setup-mac.sh\n' "$destination"
