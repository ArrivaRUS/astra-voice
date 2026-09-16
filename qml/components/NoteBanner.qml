// design/spec.md §7, «Геометрия баннеров».
import QtQuick 2.15
import ".."

Rectangle {
    id: root

    property string variant: "info"
    property string iconName: ""
    property string title: ""
    property string body: ""
    default property alias actionData: actions.data

    readonly property color ink: variant === "warn" ? Theme.warningInk
        : variant === "error" ? Theme.dangerInk
        : variant === "ok" ? Theme.successInk : Theme.fgSecondary

    // design/spec.md §7; макет 08-onboarding-1-network.html: у info иконка fg-muted, текст fg-secondary.
    readonly property color iconInk: variant === "info" ? Theme.fgMuted : ink

    width: parent ? parent.width : implicitWidth
    implicitHeight: Theme.noteBannerPaddingY * 2 + Math.max(symbol.visible ? symbol.height : 0, content.height)
    height: implicitHeight
    radius: Theme.noteBannerRadius
    color: variant === "warn" ? Theme.warningBg
        : variant === "error" ? Theme.dangerBg
        : variant === "ok" ? Theme.successBg : Theme.bgSurface2

    Icon {
        id: symbol
        x: Theme.noteBannerPaddingX
        y: Theme.noteBannerPaddingY
        name: root.iconName
        size: Theme.noteBannerIcon
        color: root.iconInk
        visible: name !== ""
    }

    Item {
        id: content
        x: Theme.noteBannerPaddingX + (symbol.visible ? symbol.width + Theme.noteBannerGap : 0)
        y: Theme.noteBannerPaddingY
        width: Math.max(0, root.width - x - Theme.noteBannerPaddingX)
        height: bodyText.y + bodyText.height + (actions.visible ? Theme.noteBannerGap + actions.height : 0)

        Text {
            id: titleText
            width: parent.width
            text: root.title
            visible: text !== ""
            height: visible ? implicitHeight : 0
            color: root.ink
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontNoteBannerSize
            font.weight: Font.Bold
            lineHeight: Theme.fontNoteBannerSize * Theme.fontNoteBannerLineHeight
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }

        Text {
            id: bodyText
            y: titleText.height + (titleText.visible ? 2 : 0) // spec §7: отступ под заголовком баннера.
            width: parent.width
            text: root.body
            visible: text !== ""
            height: visible ? implicitHeight : 0
            color: root.ink
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontNoteBannerSize
            lineHeight: Theme.fontNoteBannerSize * Theme.fontNoteBannerLineHeight
            lineHeightMode: Text.FixedHeight
            renderType: Text.NativeRendering
            textFormat: Text.PlainText
            wrapMode: Text.WordWrap
        }

        Row {
            id: actions
            y: bodyText.y + bodyText.height + Theme.noteBannerGap
            spacing: 8 // spec §7: зазор кнопок баннера.
            visible: children.length > 0
        }
    }
}
