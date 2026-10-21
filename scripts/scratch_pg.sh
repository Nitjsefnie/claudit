#!/usr/bin/env bash
# Start, export or remove a private Postgres cluster for local tests.
set -euo pipefail

usage() {
    printf 'Usage: %s start|env <dir> <port> | stop <dir> [port]\n' "$0" >&2
    exit 1
}

case "${1:-}" in
    start|env) [[ $# == 3 ]] || usage ;;
    stop) [[ $# == 2 || $# == 3 ]] || usage ;;
    *) usage ;;
esac
[[ -n $2 ]] || usage
if [[ $# == 3 && ! $3 =~ ^[0-9]+$ ]]; then
    usage
fi

action=$1
dir=$2
[[ $dir == /* ]] || dir=$PWD/$dir
port=${3:-}
marker=$dir/.scratch_pg
if [[ $action == stop && ! -f $marker ]]; then
    printf 'Refusing to remove %s: scratch_pg marker is absent.\n' "$dir" >&2
    exit 1
fi

runner=()
pg_user=$(id -un)
if [[ $(id -u) == 0 ]]; then
    if ! pg_user=$(id -un postgres); then
        printf 'Running as root requires a postgres user.\n' >&2
        exit 1
    fi
    runner=(runuser -u postgres --)
fi

exports() {
    printf 'export PGHOST=%q\nexport PGPORT=%q\nexport PGUSER=%q\n' "$dir" "$port" "$pg_user"
}

if [[ $action == env ]]; then
    exports
    exit 0
fi

pg_bindir=
pg_config_bin=$(command -v pg_config) || pg_config_bin=
if [[ -n $pg_config_bin ]]; then
    if ! pg_bindir=$("$pg_config_bin" --bindir); then
        pg_bindir=
    fi
fi
find_binary() {
    if [[ -n $pg_bindir && -x $pg_bindir/$1 ]]; then
        printf '%s\n' "$pg_bindir/$1"
        return 0
    fi
    binary=$(command -v "$1") || binary=
    if [[ -n $binary ]]; then
        printf '%s\n' "$binary"
    else
        printf 'Cannot find %s: tried pg_config --bindir and PATH.\n' "$1" >&2
        return 1
    fi
}
pg_ctl=$(find_binary pg_ctl)
initdb=$(find_binary initdb)

if [[ $action == start ]]; then
    mkdir -p -- "$dir"
    if [[ ${#runner[@]} != 0 ]]; then
        chown postgres -- "$dir"
    fi
    if [[ ! -f $dir/data/PG_VERSION ]]; then
        "${runner[@]}" "$initdb" -D "$dir/data" >&2
    fi
    touch -- "$marker"
    socket_dir=${dir//\'/\'\\\'\'}
    "${runner[@]}" "$pg_ctl" -D "$dir/data" -l "$dir/log" \
        -o "-k '$socket_dir' -p $port -c listen_addresses=" start >&2
    exports
else
    "${runner[@]}" "$pg_ctl" -D "$dir/data" -m fast stop >&2
    rm -rf -- "$dir"
fi
