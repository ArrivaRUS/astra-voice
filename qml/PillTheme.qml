// СГЕНЕРИРОВАНО scripts/gen_theme.py из design/tokens.json 2.4.0 — НЕ ПРАВИТЬ РУКАМИ.
// Перегенерация: python3 scripts/gen_theme.py · проверка: python3 scripts/gen_theme.py --check

pragma Singleton
import QtQuick 2.15

QtObject {
    // ── color.fixed: рисуется поверх чужого рабочего стола, с темой НЕ меняется
    readonly property color appiconAccent: "#2FD9C4"
    readonly property color appiconBg: "#1B3A73"
    readonly property color appiconMark: "#F2F5FA"
    readonly property color notifyBg: "#1B2331"
    readonly property color notifyBorder: Qt.rgba(1, 1, 1, 0.12)
    readonly property color notifyBtnBg: Qt.rgba(1, 1, 1, 0.1)
    readonly property color notifyBtnPrimaryBg: "#6C93E8"
    readonly property color notifyBtnPrimaryFg: "#0B1220"
    readonly property color notifyFg: "#F2F5FA"
    readonly property color notifyFgMuted: "#8C97AC"
    readonly property color pillBarFlat: "#8C97AC"
    readonly property color pillBarLive: "#12B3A0"
    readonly property color pillBg: Qt.rgba(0.054902, 0.090196, 0.160784, 0.94)
    readonly property color pillCloseBg: Qt.rgba(1, 1, 1, 0.12)
    readonly property color pillCloseFg: "#C4CDDC"
    readonly property color pillDone: "#4FBF88"
    readonly property color pillError: "#F0645F"
    readonly property color pillFg: "#F2F5FA"
    readonly property color pillIconMuted: "#C4CDDC"
    readonly property color pillIconNeutral: "#8C97AC"
    readonly property color pillProcessing: "#E8A33A"
    readonly property color pillWarning: "#F2B559"
    readonly property color tooltipBg: "#0E1729"
    readonly property color tooltipFg: "#F2F5FA"

    // ── component.pill
    readonly property real pillBarAreaH: 20
    readonly property real pillBarGap: 3
    readonly property real pillBarHFlat: 4
    readonly property real pillBarHMax: 20
    readonly property real pillBarHMin: 3
    readonly property real pillBarRadius: 3
    readonly property real pillBarW: 6
    readonly property int pillBars: 9
    readonly property real pillBarsBlockW: 78
    readonly property real pillGap: 9
    readonly property real pillH: 36
    readonly property real pillMaxW: 320
    readonly property real pillMeasuredWListening: 187.6
    readonly property real pillMeetingBarBtnGap: 5
    readonly property real pillMeetingBarBtnH: 26
    readonly property real pillMeetingBarBtnIcon: 12
    readonly property real pillMeetingBarBtnPaddingY: 0
    readonly property real pillMeetingBarBtnPaddingX: 10
    readonly property real pillMeetingBarBtnRadius: 13
    readonly property real pillMeetingBarCompactW: 141.6
    readonly property real pillMeetingBarGap: 8
    readonly property int pillMeetingBarGripCols: 2
    readonly property real pillMeetingBarGripDot: 2
    readonly property real pillMeetingBarGripH: 16
    readonly property real pillMeetingBarGripOpacity: 0.55
    readonly property int pillMeetingBarGripRows: 3
    readonly property real pillMeetingBarGripW: 8
    readonly property real pillMeetingBarIconBtn: 26
    readonly property int pillMeetingBarLabelWeight: 700
    readonly property real pillMeetingBarMaxW: 524
    readonly property real pillMeetingBarMinW: 340
    readonly property real pillMeetingBarMultilineGap: 2
    readonly property real pillMeetingBarMultilineHMin: 63.8
    readonly property real pillMeetingBarMultilineIndent: 22
    readonly property real pillMeetingBarMultilinePaddingTop: 0
    readonly property real pillMeetingBarMultilinePaddingRight: 5
    readonly property real pillMeetingBarMultilinePaddingBottom: 7
    readonly property real pillMeetingBarMultilinePaddingLeft: 8
    readonly property real pillMeetingBarMultilineRadius: 14
    readonly property real pillMeetingBarNoticeGap: 8
    readonly property real pillMeetingBarPaddingTop: 0
    readonly property real pillMeetingBarPaddingRight: 5
    readonly property real pillMeetingBarPaddingBottom: 0
    readonly property real pillMeetingBarPaddingLeft: 8
    readonly property real pillMeetingBarRow1MeasuredW: 481.7
    readonly property real pillMeetingBarRow2GapY: 6
    readonly property real pillMeetingBarRow2GapX: 8
    readonly property real pillMeetingBarRow2PaddingRight: 4
    readonly property real pillMeetingBarRow2TextBasis: 200
    readonly property real pillMeetingBarScreenOffset: 12
    readonly property real pillMeetingBarSepH: 18
    readonly property real pillMeetingBarSepW: 1
    readonly property real pillMeetingBarSize: 12.5
    readonly property real pillMeetingBarSymbol: 14
    readonly property real pillMeetingBarTimerSize: 13
    readonly property real pillMeetingBarTimerTracking: 0.02
    readonly property real pillMeetingNoticeAdminMarginTop: 4
    readonly property real pillMeetingNoticeAdminSize: 11.5
    readonly property real pillMeetingNoticeFootMarginTop: 6
    readonly property real pillMeetingNoticeFootPaddingTop: 6
    readonly property real pillMeetingNoticeHeadIcon: 13
    readonly property real pillMeetingNoticeHintMarginTop: 8
    readonly property real pillMeetingNoticePaddingY: 12
    readonly property real pillMeetingNoticePaddingX: 14
    readonly property real pillMeetingNoticeRadius: 10
    readonly property real pillMeetingNoticeTextboxBorder: 1
    readonly property real pillMeetingNoticeTextboxMarginTop: 8
    readonly property real pillMeetingNoticeTextboxMarginX: 0
    readonly property real pillMeetingNoticeTextboxMarginBottom: 2
    readonly property real pillMeetingNoticeTextboxPaddingY: 8
    readonly property real pillMeetingNoticeTextboxPaddingX: 10
    readonly property real pillMeetingNoticeTextboxRadius: 7
    readonly property real pillMeetingNoticeTextboxSize: 13
    readonly property real pillMeetingNoticeW: 340
    readonly property real pillMeetingOfferBodyLineHeight: 1.45
    readonly property real pillMeetingOfferBodySize: 12.5
    readonly property real pillMeetingOfferBtnIcon: 12
    readonly property real pillMeetingOfferButtonsMarginTop: 10
    readonly property real pillMeetingOfferClose: 18
    readonly property real pillMeetingOfferCloseIcon: 10
    readonly property real pillMeetingOfferCounterSize: 11.5
    readonly property real pillMeetingOfferH: 148
    readonly property real pillMeetingOfferHeadGap: 8
    readonly property real pillMeetingOfferHeadIcon: 13
    readonly property real pillMeetingOfferHeadMarginBottom: 6
    readonly property real pillMeetingOfferPaddingY: 12
    readonly property real pillMeetingOfferPaddingX: 14
    readonly property real pillMeetingOfferRadius: 10
    readonly property real pillMeetingOfferScreenOffset: 12
    readonly property real pillMeetingOfferTimerH: 2
    readonly property real pillMeetingOfferTitleMarginBottom: 3
    readonly property int pillMeetingOfferTitleWeight: 500
    readonly property real pillMeetingOfferW: 380
    readonly property real pillMinW: 172
    readonly property real pillRadius: 18
}
