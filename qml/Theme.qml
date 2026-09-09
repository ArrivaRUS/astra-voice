// СГЕНЕРИРОВАНО scripts/gen_theme.py из design/tokens.json 2.1.1 — НЕ ПРАВИТЬ РУКАМИ.
// Перегенерация: python3 scripts/gen_theme.py · проверка: python3 scripts/gen_theme.py --check

pragma Singleton
import QtQuick 2.15

QtObject {
    // ── тема: dark берётся из themeSource (core/theme.py), без него — светлая
    readonly property bool dark: (typeof themeSource !== "undefined" && themeSource !== null) ? themeSource.dark === true : false

    // ── color.light / color.dark
    readonly property color accent: dark ? "#2FD9C4" : "#12B3A0"
    readonly property color accent300: "#3FCDBB"
    readonly property color accentBg: dark ? Qt.rgba(0.184314, 0.85098, 0.768627, 0.13) : "#E3F7F4"
    readonly property color accentInk: dark ? "#2FD9C4" : "#0A776A"
    readonly property color bgApp: dark ? "#0B1220" : "#F7F9FC"
    readonly property color bgSurface: dark ? "#151E30" : "#FFFFFF"
    readonly property color bgSurface2: dark ? "#080D18" : "#EDF1F7"
    readonly property color bgTitlebar: dark ? "#101A2B" : "#E7ECF4"
    readonly property color border: dark ? Qt.rgba(1, 1, 1, 0.1) : "#D7DEEA"
    readonly property color borderSoft: dark ? Qt.rgba(1, 1, 1, 0.06) : "#EDF1F7"
    readonly property color dangerBg: dark ? Qt.rgba(0.941176, 0.392157, 0.372549, 0.13) : "#FBECEB"
    readonly property color dangerInk: dark ? "#F0645F" : "#C0322F"
    readonly property color fg: dark ? "#F2F5FA" : "#0E1729"
    readonly property color fgDisabled: dark ? "#7E889B" : "#616D83"
    readonly property color fgFaint: dark ? "#5A6884" : "#AAB4C7"
    readonly property color fgMuted: dark ? "#8C97AC" : "#5A6884"
    readonly property color fgSecondary: dark ? "#C4CDDC" : "#243350"
    readonly property color focusRing: dark ? "#6C93E8" : "#1B3A73"
    readonly property color primary: dark ? "#6C93E8" : "#1B3A73"
    readonly property color primaryBg: dark ? Qt.rgba(0.423529, 0.576471, 0.909804, 0.12) : "#E8EDF7"
    readonly property color primaryFg: dark ? "#0B1220" : "#FFFFFF"
    readonly property color primaryHover: dark ? "#8AACF0" : "#12294F"
    readonly property color selectionBg: dark ? Qt.rgba(0.423529, 0.576471, 0.909804, 0.12) : "#E8EDF7"
    readonly property color selectionFg: dark ? "#6C93E8" : "#1B3A73"
    readonly property color shadow: dark ? Qt.rgba(0, 0, 0, 0.55) : Qt.rgba(0.054902, 0.090196, 0.160784, 0.18)
    readonly property color sheetCanvas: dark ? "#05080F" : "#DDE3EE"
    readonly property color sheetCanvasFg: dark ? "#8C97AC" : "#3A4761"
    readonly property color successBg: dark ? Qt.rgba(0.309804, 0.74902, 0.533333, 0.13) : "#E7F5EE"
    readonly property color successInk: dark ? "#4FBF88" : "#1F7D50"
    readonly property color warningBg: dark ? Qt.rgba(0.94902, 0.709804, 0.34902, 0.13) : "#FAF0DC"
    readonly property color warningInk: dark ? "#F2B559" : "#8F5E12"

    // ── color.state.light / color.state.dark
    readonly property color indicatorDone: dark ? "#4FBF88" : "#2FA36B"
    readonly property color indicatorError: dark ? "#F0645F" : "#D64545"
    readonly property color indicatorIdleFallback: dark ? "#8C97AC" : "#5A6884"
    readonly property color indicatorListening: dark ? "#2FD9C4" : "#12B3A0"
    readonly property color indicatorProcessing: dark ? "#F2B559" : "#E8A33A"

    // ── state.light / state.dark
    readonly property color stateFocusRing: dark ? "#2FD9C4" : "#0A776A"
    readonly property color stateFocusRingLiteral: "#12B3A0"
    readonly property color stateHoverOnSurface: dark ? "#232C3C" : "#EDF1F7"
    readonly property color stateHoverOnSurface2: dark ? "#141924" : "#E7ECF4"
    readonly property color statePressedOnSurface: dark ? "#252E3E" : "#E7ECF4"
    readonly property color statePressedOnSurface2: dark ? "#1E232D" : "#DDE3EE"
    readonly property color statePrimaryHover: dark ? "#789CEA" : "#19356A"
    readonly property color statePrimaryPressed: dark ? "#8AACF0" : "#173263"

    // ── state.focus
    readonly property real focusOffset: 2
    readonly property real focusRadius: 8
    readonly property real focusWidth: 2

    // ── font.family
    readonly property string fontMono: "PT Mono"
    readonly property var fontMonoStack: ["PT Mono", "DejaVu Sans Mono", "Liberation Mono", "monospace"]
    readonly property string fontUi: "PT Root UI"
    readonly property var fontUiStack: ["PT Root UI", "PT Astra Sans", "Open Sans", "Roboto", "sans-serif"]

    // ── font.weight
    readonly property int weightBold: 700
    readonly property int weightMedium: 500
    readonly property int weightRegular: 400

    // ── font.role
    readonly property real fontBadgeSize: 11.5
    readonly property int fontBadgeWeight: 500
    readonly property real fontBodyLineHeight: 1.5
    readonly property real fontBodySize: 14
    readonly property int fontBodyWeight: 400
    readonly property real fontButtonSize: 13
    readonly property int fontButtonWeight: 500
    readonly property real fontButtonSmSize: 12.5
    readonly property int fontButtonSmWeight: 500
    readonly property real fontCaptionLineHeight: 1.4
    readonly property real fontCaptionSize: 12
    readonly property int fontCaptionWeight: 400
    readonly property real fontChipSize: 12.5
    readonly property int fontChipWeight: 400
    readonly property real fontFieldSize: 13
    readonly property int fontFieldWeight: 400
    readonly property real fontGroupCapsSize: 11
    readonly property real fontGroupCapsTracking: 0.08
    readonly property int fontGroupCapsWeight: 500
    readonly property real fontH1WindowLineHeight: 1.2
    readonly property real fontH1WindowSize: 28
    readonly property int fontH1WindowWeight: 700
    readonly property real fontH2SectionLineHeight: 1.25
    readonly property real fontH2SectionSize: 20
    readonly property int fontH2SectionWeight: 700
    readonly property real fontH3SubsectionLineHeight: 1.3
    readonly property real fontH3SubsectionSize: 16
    readonly property int fontH3SubsectionWeight: 500
    readonly property real fontHotkeyCaptureSize: 14
    readonly property int fontHotkeyCaptureWeight: 400
    readonly property real fontHotkeyKeySize: 13
    readonly property real fontHotkeyKeyTracking: 0.02
    readonly property int fontHotkeyKeyWeight: 400
    readonly property real fontMenuItemSize: 13
    readonly property int fontMenuItemWeight: 400
    readonly property real fontMenuShortcutSize: 11.5
    readonly property int fontMenuShortcutWeight: 400
    readonly property real fontMetricSize: 11.5
    readonly property int fontMetricWeight: 400
    readonly property real fontModelFooterSize: 12
    readonly property int fontModelFooterWeight: 400
    readonly property real fontModelNameSize: 15
    readonly property int fontModelNameWeight: 500
    readonly property real fontModelPurposeSize: 12.5
    readonly property int fontModelPurposeWeight: 400
    readonly property real fontModelVendorSize: 12.5
    readonly property int fontModelVendorWeight: 400
    readonly property real fontMonoInlineTracking: 0.02
    readonly property real fontNavCounterSize: 11.5
    readonly property int fontNavCounterWeight: 400
    readonly property real fontNavItemSize: 13.5
    readonly property int fontNavItemWeight: 500
    readonly property real fontNavItemSubSize: 13
    readonly property int fontNavItemSubWeight: 400
    readonly property real fontNoteBannerLineHeight: 1.45
    readonly property real fontNoteBannerSize: 12.5
    readonly property int fontNoteBannerWeight: 400
    readonly property real fontPillSize: 12.5
    readonly property int fontPillWeight: 400
    readonly property real fontSegmentedSize: 12.5
    readonly property int fontSegmentedWeight: 400
    readonly property real fontSelectSize: 13
    readonly property int fontSelectWeight: 400
    readonly property real fontSettingLabelLineHeight: 1.5
    readonly property real fontSettingLabelSize: 14
    readonly property int fontSettingLabelWeight: 400
    readonly property real fontSettingSubLineHeight: 1.4
    readonly property real fontSettingSubSize: 12
    readonly property int fontSettingSubWeight: 400
    readonly property real fontSmallLineHeight: 1.45
    readonly property real fontSmallSize: 13
    readonly property int fontSmallWeight: 400
    readonly property real fontStatusbarSize: 12.5
    readonly property int fontStatusbarWeight: 400
    readonly property real fontStatusbarNumSize: 12
    readonly property int fontStatusbarNumWeight: 400
    readonly property real fontTitlebarSize: 12.5
    readonly property int fontTitlebarWeight: 500

    // ── space
    readonly property real spaceCardRowDivider: 1
    readonly property real spaceGroupCaptionGap: 5
    readonly property real spaceGroupGap: 8
    readonly property real spaceHeadGap: 8
    readonly property real spaceRowGap: 10
    readonly property var spaceScale: [2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 18, 20, 22, 26, 40]
    readonly property real spaceStep: 4
    readonly property real spaceWindowContentBottom: 16
    readonly property real spaceWindowContentTop: 12
    readonly property real spaceWindowContentX: 22

    // ── size
    readonly property real sizeBodyH: 552
    readonly property real sizeContentColMax: 760
    readonly property real sizeContentColW: 672
    readonly property real sizeSheetMaxW: 900
    readonly property real sizeSidebarW: 184
    readonly property real sizeStatusbarH: 36
    readonly property real sizeTitlebarH: 32
    readonly property real sizeWindowH: 620
    readonly property real sizeWindowMaxUsefulW: 988
    readonly property real sizeWindowMinH: 560
    readonly property real sizeWindowMinW: 900
    readonly property real sizeWindowW: 900

    // ── radius
    readonly property real radiusBadge: 5
    readonly property real radiusCard: 10
    readonly property real radiusChip: 14
    readonly property real radiusControl: 7
    readonly property real radiusDialog: 9
    readonly property real radiusHotkeyKey: 6
    readonly property real radiusLockBadge: 5
    readonly property real radiusMenu: 8
    readonly property real radiusMenuItem: 6
    readonly property real radiusModelCard: 10
    readonly property real radiusNavItem: 7
    readonly property real radiusNote: 9
    readonly property real radiusPill: 18
    readonly property real radiusPopover: 8
    readonly property real radiusToggle: 11
    readonly property real radiusTooltip: 6
    readonly property real radiusTrack: 3
    readonly property real radiusWindow: 8
    readonly property real radiusWindowBtn: 3

    // ── border
    readonly property real borderDivider: 1
    readonly property real borderFocus: 2
    readonly property real borderFocusOffset: 2
    readonly property real borderHairline: 1
    readonly property real borderHotkeyKeyBottom: 2

    // ── motion.duration
    readonly property int durationClipboardPostPaste: 100
    readonly property int durationClipboardPrePaste: 50
    readonly property int durationEnter: 160
    readonly property int durationExit: 120
    readonly property int durationHover: 120
    readonly property int durationListeningPulse: 1200
    readonly property int durationLoadingModelMax: 5000
    readonly property int durationMicRetry: 300
    readonly property int durationPillAppear: 100
    readonly property int durationPillCancelled: 800
    readonly property int durationPillClipboard: 1200
    readonly property int durationPillDone: 500
    readonly property int durationPillEmpty: 1000
    readonly property int durationPillError: 3000
    readonly property int durationPillLimit: 2000
    readonly property int durationPillTooShort: 150
    readonly property int durationSilenceThreshold: 2000
    readonly property int durationSpinnerRev: 1000
    readonly property int durationTrayDone: 800
    readonly property int durationUptodateMessage: 3000

    // ── motion.easing
    readonly property var easingEnter: [0.2, 0, 0, 1]
    readonly property var easingExit: [0.4, 0, 1, 1]
    readonly property var easingHover: [0.2, 0, 0, 1]

    // ── component
    readonly property real badgeGap: 4
    readonly property real badgeHeight: 21.3
    readonly property int badgeMaxPerCard: 2
    readonly property real badgePaddingY: 2
    readonly property real badgePaddingX: 7
    readonly property real badgeRadius: 5
    readonly property real badgeSize: 11.5
    readonly property int badgeWeight: 500
    readonly property real buttonBorder: 1
    readonly property real buttonGap: 6
    readonly property real buttonGhostPaddingY: 6
    readonly property real buttonGhostPaddingX: 8
    readonly property real buttonHeight: 33.5
    readonly property real buttonHeightSm: 28.1
    readonly property real buttonIconSize: 13
    readonly property real buttonPaddingY: 6
    readonly property real buttonPaddingX: 12
    readonly property real buttonRadius: 7
    readonly property real buttonSmPaddingY: 4
    readonly property real buttonSmPaddingX: 9
    readonly property real cardBorder: 1
    readonly property real cardRadius: 10
    readonly property real cardRowMinH: 38
    readonly property real cardRowPaddingY: 7
    readonly property real cardRowPaddingX: 14
    readonly property real chipGap: 6
    readonly property real chipPaddingY: 4
    readonly property real chipPaddingX: 11
    readonly property real chipRadius: 14
    readonly property real dialogBodyGap: 13
    readonly property real dialogBodyPaddingTop: 16
    readonly property real dialogBodyPaddingX: 18
    readonly property real dialogBodyPaddingBottom: 6
    readonly property real dialogFooterGap: 8
    readonly property real dialogFooterPaddingTop: 12
    readonly property real dialogFooterPaddingX: 18
    readonly property real dialogFooterPaddingBottom: 14
    readonly property real dialogHeaderH: 30
    readonly property real dialogRadius: 9
    readonly property real dialogW: 420
    readonly property real fieldBorder: 1
    readonly property color fieldFgPlaceholder: fgMuted
    readonly property real fieldGap: 8
    readonly property real fieldPaddingY: 7
    readonly property real fieldPaddingX: 11
    readonly property real fieldRadius: 7
    readonly property real hintSize: 15
    readonly property color hotkeyChipBg: bgSurface2
    readonly property color hotkeyChipFg: fg
    readonly property real hotkeyChipHeight: 26.5
    readonly property real hotkeyChipPaddingTop: 3
    readonly property real hotkeyChipPaddingX: 8
    readonly property real hotkeyChipPaddingBottom: 2
    readonly property real hotkeyChipRadius: 6
    readonly property color lockBadgeBg: bgSurface2
    readonly property color lockBadgeFg: fgMuted
    readonly property real lockBadgeIcon: 11
    readonly property real lockBadgePaddingY: 2
    readonly property real lockBadgePaddingX: 6
    readonly property real lockBadgeRadius: 5
    readonly property color menuDisabledFg: fgDisabled
    readonly property real menuHeaderSize: 12
    readonly property real menuItemGap: 9
    readonly property real menuItemPaddingY: 6
    readonly property real menuItemPaddingX: 9
    readonly property real menuItemRadius: 6
    readonly property real menuPadding: 5
    readonly property real menuRadius: 8
    readonly property real menuW: 262
    readonly property real micLevelMeterBarGap: 3
    readonly property real micLevelMeterBarRadius: 3
    readonly property real micLevelMeterBarW: 7
    readonly property int micLevelMeterBars: 9
    readonly property color micLevelMeterBoxBg: bgSurface2
    readonly property real micLevelMeterBoxPaddingY: 10
    readonly property real micLevelMeterBoxPaddingX: 12
    readonly property real micLevelMeterBoxRadius: 8
    readonly property color micLevelMeterColorFlat: fgFaint
    readonly property color micLevelMeterColorLive: accent
    readonly property real micLevelMeterH: 34
    readonly property var micLevelMeterSampleFlat: [3, 3, 3, 3, 3, 3, 3, 3, 3]
    readonly property var micLevelMeterSampleLive: [9, 16, 26, 31, 20, 12, 22, 15, 8]
    readonly property var micLevelMeterSampleQuiet: [5, 7, 6, 8, 6, 5, 7, 6, 5]
    readonly property real modelCardBadgesGap: 5
    readonly property real modelCardMarginBottom: 8
    readonly property color modelCardMetricFillEstimated: fgFaint
    readonly property color modelCardMetricFillMeasured: primary
    readonly property real modelCardMetricGap: 8
    readonly property real modelCardMetricLabelW: 54
    readonly property color modelCardMetricTrackBg: bgSurface2
    readonly property real modelCardMetricTrackH: 6
    readonly property real modelCardMetricTrackRadius: 3
    readonly property real modelCardMetricTrackW: 78
    readonly property real modelCardMetricValueSize: 11.5
    readonly property real modelCardMetricsGap: 5
    readonly property real modelCardPaddingY: 9
    readonly property real modelCardPaddingX: 13
    readonly property real modelCardRadius: 10
    readonly property real modelCardTopGap: 14
    readonly property real noteBannerGap: 9
    readonly property real noteBannerIcon: 15
    readonly property real noteBannerPaddingY: 10
    readonly property real noteBannerPaddingX: 12
    readonly property real noteBannerRadius: 9
    readonly property real notificationButtonPaddingY: 5
    readonly property real notificationButtonPaddingX: 10
    readonly property real notificationButtonRadius: 6
    readonly property real notificationButtonsGap: 7
    readonly property real notificationHeaderSize: 11.5
    readonly property real notificationPaddingY: 11
    readonly property real notificationPaddingX: 12
    readonly property real notificationRadius: 8
    readonly property real notificationTitleSize: 13.5
    readonly property real notificationW: 340
    readonly property real onboardingBarH: 60
    readonly property real onboardingDot: 7
    readonly property real onboardingDotActiveRadius: 4
    readonly property real onboardingDotActiveW: 20
    readonly property real onboardingDotsGap: 7
    readonly property bool onboardingSkippable: true
    readonly property int onboardingSteps: 5
    readonly property real popoverItemDescSize: 11.5
    readonly property real popoverItemGap: 8
    readonly property real popoverItemH: 38
    readonly property real popoverItemPaddingY: 7
    readonly property real popoverItemPaddingX: 9
    readonly property real popoverItemRadius: 6
    readonly property real popoverMaxH: 238
    readonly property int popoverMaxVisibleItems: 6
    readonly property real popoverMinW: 236
    readonly property real popoverPadding: 5
    readonly property real popoverRadius: 8
    readonly property int popoverZ: 5
    readonly property color progressBg: bgSurface2
    readonly property color progressFill: primary
    readonly property real progressH: 6
    readonly property real progressRadius: 3
    readonly property real progressStatusbarW: 120
    readonly property color scrollbarColor: fgFaint
    readonly property real scrollbarOpacity: 0.45
    readonly property bool scrollbarOverlay: true
    readonly property real scrollbarRadius: 4
    readonly property real scrollbarRight: 4
    readonly property real scrollbarW: 8
    readonly property real segmentedBorder: 1
    readonly property real segmentedHeight: 30.8
    readonly property color segmentedItemFg: fgSecondary
    readonly property real segmentedItemH: 28.8
    readonly property real segmentedItemPaddingY: 5
    readonly property real segmentedItemPaddingX: 11
    readonly property real segmentedRadius: 7
    readonly property real selectChevron: 13
    readonly property real selectGap: 8
    readonly property real selectHeight: 33.5
    readonly property color selectOpenState: focusRing
    readonly property real selectPaddingY: 6
    readonly property real selectPaddingX: 10
    readonly property real selectRadius: 7
    readonly property color sidebarBg: bgSurface2
    readonly property color sidebarCounterOnActive: dark ? Qt.rgba(0.043137, 0.070588, 0.12549, 0.85) : Qt.rgba(1, 1, 1, 0.75)
    readonly property real sidebarItemGap: 2
    readonly property real sidebarItemH: 34.3
    readonly property real sidebarItemIcon: 17
    readonly property real sidebarItemIconGap: 9
    readonly property real sidebarItemPaddingY: 7
    readonly property real sidebarItemPaddingX: 9
    readonly property real sidebarItemRadius: 7
    readonly property real sidebarLogoMarginBottom: 6
    readonly property real sidebarLogoMarkW: 22
    readonly property real sidebarLogoPaddingTop: 4
    readonly property real sidebarLogoPaddingX: 6
    readonly property real sidebarLogoPaddingBottom: 12
    readonly property real sidebarLogoWordmarkSize: 15
    readonly property real sidebarNavMarginTop: 8
    readonly property real sidebarPaddingY: 12
    readonly property real sidebarPaddingX: 10
    readonly property real sidebarW: 184
    readonly property bool soundDefault: false
    readonly property int soundDurationMax: 120
    readonly property color statusbarAccentFg: primary
    readonly property color statusbarBg: bgApp
    readonly property color statusbarFg: fgMuted
    readonly property real statusbarGap: 10
    readonly property real statusbarH: 36
    readonly property real statusbarSeparatorDot: 4
    readonly property real statusbarTrayIcon: 15
    readonly property color titlebarCloseColor: dark ? "#8C3B36" : "#D96B62"
    readonly property real titlebarGap: 8
    readonly property real titlebarH: 32
    readonly property real titlebarMarkW: 16
    readonly property color toggleDisabledBg: border
    readonly property real toggleH: 21
    readonly property real toggleKnob: 17
    readonly property color toggleKnobBg: "#FFFFFF"
    readonly property real toggleKnobInset: 2
    readonly property real toggleKnobOnX: 19
    readonly property color toggleLockedBg: border
    readonly property color toggleOffBg: fgFaint
    readonly property color toggleOnBg: primary
    readonly property real toggleRadius: 11
    readonly property real toggleW: 38
    readonly property real updatePanelListLineHeight: 1.6
    readonly property real updatePanelListSize: 12.5
    readonly property real updatePanelPaddingY: 14
    readonly property real updatePanelPaddingX: 16
    readonly property real updatePanelRadius: 10
}
