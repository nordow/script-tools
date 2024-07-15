#!/bin/bash

function info() {
    echo "info: $1"
}

function warning() {
    echo "warning: $1"
}

function error() {
    echo "error: $1"
}

function throw() {
    exit 1
}

function command_exists() {
    command -v "$@" > /dev/null 2>&1
}

function install() {
    info "chrome installation start..."

    if command_exists "google-chrome"; then
        warning "chrome exists, reinstall is unsupported, exit."

        return
    fi

    info "apt-get update and upgrade..."

    apt-get -y update && apt-get -y upgrade

    if [ $? -ne 0 ]; then
        error "apt-get update and upgrade failed."
        throw
    fi

    local version=$([ "${1}" ] && echo "${1}" || echo "latest")

    info "chrome target version is '${version}'."

    local url=$([ "${version}" != "latest" ] \
                && echo "https://dl.google.com/linux/chrome/deb/pool/main/g/google-chrome-stable/google-chrome-stable_${version}-1_amd64.deb" \
                || echo "https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb")

    info "chrome download from '${url}'..."

    local file_name="google-chrome-stable_amd64.deb"

    wget -O "${file_name}" "${url}"

    if [ $? -ne 0 ]; then
        error "chrome download failed."
        throw
    fi

    info "chrome install by apt-get..."

    apt-get -y install "./${file_name}"

    if [ $? -ne 0 ]; then
        error "chrome install failed."
        throw
    fi

    if command_exists "google-chrome"; then
        info "chrome installation succeed, version is '${version}'."
    else
        warning "chrome installation should succeed, but 'google-chrome' command does not exist."
    fi
}

function uninit() {
    popd > /dev/null
    
    rm -rf "${1}"
}

function init() {
    mkdir -p "${1}"

    pushd "${1}" > /dev/null

    trap "uninit '${1}'" EXIT
}

work_dir="/tmp/get-chrome"

init "${work_dir}"

install "${CHROME_VERSION}"
