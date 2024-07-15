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
    info "chromedriver installation start..."

    if command_exists "chromedriver"; then
        warning "chromedriver exists, reinstall is unsupported, exit."

        return
    fi

    local version=$1

    if [ -z "${version}" ]; then
        local full_version="$(google-chrome --version)"

        if [ $? -ne 0 ]; then
            error "cannot determine chromedriver version."
            throw
        fi

        local trimmed_full_version="$(echo ${full_version} | xargs)"

        version="${trimmed_full_version##* }"
    fi

    info "chromedriver target version is '${version}'."

    [ $((${version%%.*})) -ge 115 ]

    local version_115_or_greater=$?
    local url=$([ $version_115_or_greater -eq 0 ] \
                && echo "https://storage.googleapis.com/chrome-for-testing-public/${version}/linux64/chromedriver-linux64.zip" \
                || echo "https://chromedriver.storage.googleapis.com/${version}/chromedriver_linux64.zip")

    info "chromedriver download from '${url}'..."

    local file_name="chromedriver_linux64.zip"

    wget -O "${file_name}" "${url}"

    if [ $? -ne 0 ]; then
        error "chromedriver download failed."
        throw
    fi

    info "chromedriver unzip into '/usr/bin'..."

    local path_in_zip=$([ $version_115_or_greater -eq 0 ] \
                        && echo "*/chromedriver" \
                        || echo "chromedriver")

    unzip -j "${file_name}" "${path_in_zip}" -d "/usr/bin"

    if [ $? -ne 0 ]; then
        error "chromedriver unzip failed."
        throw
    fi

    if command_exists "chromedriver"; then
        info "chromedriver installation succeed, version is '${version}'."
    else
        warning "chromedriver installation should succeed, but 'chromedriver' command does not exist."
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

work_dir="/tmp/get-chromedriver"

init "${work_dir}"

install "${CHROMEDRIVER_VERSION}"
