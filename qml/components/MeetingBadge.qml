// Бейдж встречи — design/spec.md §4.8, design/meetings/tokens.md §2.4/§3.1.
// Форма + текст, не только цвет. Хвост после « · » (таймер, проценты) — PT Mono.
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
    implicitHeight: Math.max(Theme.badgeHeight, label.implicitHeight + Theme.badgePaddingY * 2)
    width: implicitWidth
    height: implicitHeight
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
            text: root.monoAt >= 0 ? root.text.slice(0, root.monoAt) : root.text
            textFormat: Text.PlainText
            color: root.ink
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontBadgeSize
            font.weight: Font.Medium
            renderType: Text.NativeRendering
        }
        Text {
            objectName: "meetingBadgeMono"
            visible: root.monoAt >= 0 || root.mono !== ""
            // Как в строках M2/M3: «Запись» + моно «· 0:42:10» (разделитель — моно-хвост).
            text: root.mono !== "" ? root.mono : root.monoAt >= 0 ? root.text.slice(root.monoAt + 1) : ""
            textFormat: Text.PlainText
            color: root.ink
            font.family: Theme.fontMono
            font.pixelSize: Theme.fontBadgeSize
            renderType: Text.NativeRendering
        }
    }
}
