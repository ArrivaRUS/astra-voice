// Бейдж встречи — design/spec.md §4.8, design/meetings/tokens.md §2.4/§3.1.
// Форма + текст, не только цвет. Хвост после « · » (таймер, проценты) — PT Mono;
// сам разделитель «·» — шрифтом интерфейса (макеты: «Расшифровка · <mono>40 %</mono>»).
import QtQuick 2.15
import ".."

Rectangle {
    id: root
    objectName: "meetingBadge"

    property string text: ""
    // Явный моно-хвост («0:31:10–0:34:02» у отметок); иначе — всё после « · ».
    property string mono: ""
    // neutral | primary | success | warning | danger
    property string kind: "neutral"
    property string iconName: ""
    property bool iconFill: iconName === "rec"

    readonly property int monoAt: mono !== "" ? -1 : text.indexOf(" · ")
    readonly property color ink: kind === "success" ? Theme.successInk
        : (kind === "danger" || kind === "error") ? Theme.dangerInk
        : kind === "warning" ? Theme.warningInk
        : kind === "primary" ? Theme.primary : Theme.fgSecondary

    color: kind === "success" ? Theme.successBg
        : (kind === "danger" || kind === "error") ? Theme.dangerBg
        : kind === "warning" ? Theme.warningBg
        : kind === "primary" ? Theme.primaryBg : Theme.bgSurface2
    radius: Theme.badgeRadius
    implicitWidth: label.implicitWidth + Theme.badgePaddingX * 2
    // Однострочный: высота — из токенов (Text округляет свою строку 17.25 вверх до 18).
    implicitHeight: Math.max(Theme.badgeHeight, Theme.fontBadgeSize * Theme.fontBodyLineHeight + Theme.badgePaddingY * 2)
    width: implicitWidth
    // Браузер снапит 21.25 к 21 пикселю; дробная высота даёт строку-полутень (22).
    height: Math.round(implicitHeight)
    Accessible.role: Accessible.StaticText
    Accessible.name: mono !== "" ? text + " " + mono : text

    Row {
        id: label
        anchors.centerIn: parent
        spacing: Theme.badgeGap

        Icon {
            objectName: "meetingBadgeSymbol"
            name: root.iconName
            size: 11 // design/meetings/tokens.md §8.2: badge glyph.
            strokeWidth: name === "pause" ? 2.2 : 1.5
            fill: root.iconFill
            color: root.ink
            visible: name !== ""
            anchors.verticalCenter: parent.verticalCenter
        }
        Text {
            objectName: "meetingBadgeText"
            text: root.monoAt >= 0 ? root.text.slice(0, root.monoAt + 2) : root.text
            textFormat: Text.PlainText
            color: root.ink
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontBadgeSize
            font.weight: Font.Medium
            // Макет: line-height 1.5 → строка 17.25, бейдж 2 + 17.25 + 2 = 21.25 (tokens h 21.3).
            lineHeight: Theme.fontBadgeSize * Theme.fontBodyLineHeight
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
        }
        Text {
            objectName: "meetingBadgeMono"
            visible: root.monoAt >= 0 || root.mono !== ""
            // «Запись ·» + зазор 4 (gap флекса в макете) + моно «0:42:10».
            text: root.mono !== "" ? root.mono : root.monoAt >= 0 ? root.text.slice(root.monoAt + 3) : ""
            textFormat: Text.PlainText
            color: root.ink
            font.family: Theme.fontMono
            font.pixelSize: Theme.fontBadgeSize
            font.letterSpacing: Theme.fontBadgeSize * Theme.fontMonoInlineTracking
            lineHeight: Theme.fontBadgeSize * Theme.fontBodyLineHeight
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
        }
    }
}
