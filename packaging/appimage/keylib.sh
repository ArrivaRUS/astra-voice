# Подключается через «.»; грамматику с paths.APPIMAGE_KEY_RE сверяет T-181.
key_ok() {
    case ${1-} in
        *-*) set -- "${1%-*}" "${1##*-}" ;;
        *) return 1 ;;
    esac
    case $2 in
        *[!0123456789abcdef]*) return 1 ;;
    esac
    case $2 in
        ????????????) ;;
        *) return 1 ;;
    esac

    case $1 in
        *~*)
            set -- "${1%%~*}" "${1#*~}"
            case $2 in
                '' | *[!ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789.]*)
                    return 1 ;;
            esac
            ;;
    esac

    case $1 in
        *.*.*) ;;
        *) return 1 ;;
    esac
    set -- "${1%%.*}" "${1#*.}"
    case $1 in
        '' | *[!0123456789]*) return 1 ;;
    esac
    set -- "${2%%.*}" "${2#*.}"
    case $1 in
        '' | *[!0123456789]*) return 1 ;;
    esac
    case $2 in
        '' | *[!0123456789]*) return 1 ;;
    esac
    return 0
}
