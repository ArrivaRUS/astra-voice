// Тост «Сохранено в «Документы»: <файл>» [Открыть папку] — design/meetings/spec.md §4.7,
// tokens.md §3.10: одна строка, ширина ≤ контент − 44, многоточие только в середине имени
// файла, цвета PillTheme (одинаковы в обеих темах), 4 с. Путь не показывается — только папка.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

Rectangle {
    id: root
    objectName: "meetingToast"

    property var bridge: null
    property real maxWidth: 752
    property bool freezeAnimations: false
    readonly property var toast: bridge && bridge.toast !== undefined ? bridge.toast : ({ "serial": 0 })
    readonly property var texts: bridge && bridge.cardTexts !== undefined ? bridge.cardTexts : ({})
    property int shownSerial: 0
    // Значок · «Сохранено в «…»:» · имя файла · кнопка — зазор 10 между всеми (макет 05 §5).
    readonly property real fixedWidth: Theme.toastPaddingLeft + Theme.toastIcon + prefix.implicitWidth
        + open.implicitWidth + Theme.toastPaddingRight + Theme.toastGap * 3

    function txt(key) { return texts[key] !== undefined ? texts[key] : "" }

    visible: false
    width: Math.min(maxWidth, fixedWidth + file.implicitWidth)
    height: Math.max(Theme.toastH, open.height + Theme.toastPaddingTop + Theme.toastPaddingBottom)
    radius: Theme.toastRadius
    color: PillTheme.tooltipBg
    z: 6 // tokens.md §3.10: над содержимым раздела.
    Accessible.role: Accessible.AlertMessage
    Accessible.name: prefix.text + file.text

    onToastChanged: {
        var serial = toast.serial || 0
        if (serial > root.shownSerial) {
            root.shownSerial = serial
            root.visible = true
            hide.restart()
        }
    }

    Timer {
        id: hide
        interval: Theme.durationMeetingToast
        onTriggered: root.visible = false
    }

    Row {
        x: Theme.toastPaddingLeft
        anchors.verticalCenter: parent.verticalCenter
        spacing: Theme.toastGap
        Icon {
            anchors.verticalCenter: parent.verticalCenter
            name: "check"
            size: Theme.toastIcon
            color: PillTheme.tooltipFg
        }
        Text {
            id: prefix
            anchors.verticalCenter: parent.verticalCenter
            text: root.txt("savedPrefix") + (root.toast.folder || "") + root.txt("savedMiddle")
            textFormat: Text.PlainText
            color: PillTheme.tooltipFg
            font.family: Theme.fontUi
            font.pixelSize: Theme.toastSize
            renderType: Text.NativeRendering
        }
        Text {
            id: file
            objectName: "meetingToastFile"
            anchors.verticalCenter: parent.verticalCenter
            width: Math.max(0, Math.min(implicitWidth, root.maxWidth - root.fixedWidth))
            text: root.toast.file || ""
            textFormat: Text.PlainText
            elide: Text.ElideMiddle
            color: PillTheme.tooltipFg
            font.family: Theme.fontUi
            font.pixelSize: Theme.toastSize
            renderType: Text.NativeRendering
        }
        AvButton {
            id: open
            objectName: "meetingToastOpen"
            anchors.verticalCenter: parent.verticalCenter
            small: true
            text: root.txt("openFolder")
            onClicked: if (root.bridge) root.bridge.openSavedFolder()
            contentItem: Text {
                text: open.text
                textFormat: Text.PlainText
                font: open.font
                color: PillTheme.tooltipFg
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                renderType: Text.NativeRendering
            }
            background: Rectangle {
                radius: Theme.buttonRadius
                color: PillTheme.pillCloseBg
                Rectangle {
                    anchors.fill: parent
                    anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
                    radius: Theme.focusRadius
                    color: "transparent"
                    border.width: Theme.focusWidth
                    border.color: Theme.stateFocusRing
                    visible: open.visualFocus
                }
            }
        }
    }
}
