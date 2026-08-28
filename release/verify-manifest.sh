#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
    printf '{"status":"blocked","error":"RELEASE_SIGNATURE_ARGUMENTS_INVALID"}\n' >&2
    exit 2
fi

manifest=$1
signature=$2
public_key=$3

if ! key_text=$(openssl rsa -pubin -in "$public_key" -text -noout 2>/dev/null); then
    printf '{"status":"blocked","error":"RELEASE_PUBLIC_KEY_NOT_RSA"}\n' >&2
    exit 2
fi
key_bits=$(printf '%s\n' "$key_text" | sed -n 's/.*Public-Key: (\([0-9][0-9]*\) bit).*/\1/p' | head -n 1)
if [ -z "$key_bits" ] || [ "$key_bits" -lt 3072 ]; then
    printf '{"status":"blocked","error":"RELEASE_RSA_KEY_TOO_SMALL"}\n' >&2
    exit 2
fi

if ! openssl dgst -sha256 -verify "$public_key" -signature "$signature" "$manifest" >/dev/null 2>&1; then
    printf '{"status":"blocked","error":"RELEASE_SIGNATURE_INVALID"}\n' >&2
    exit 2
fi

key_id=$(openssl rsa -pubin -in "$public_key" -outform DER 2>/dev/null | shasum -a 256 | awk '{print $1}')
manifest_sha256=$(shasum -a 256 "$manifest" | awk '{print $1}')
signature_sha256=$(shasum -a 256 "$signature" | awk '{print $1}')
printf '{"status":"verified","algorithm":"RSA-SHA256-PKCS1-v1_5","key_id":"%s","manifest_sha256":"%s","signature_sha256":"%s"}\n' \
    "$key_id" "$manifest_sha256" "$signature_sha256"
