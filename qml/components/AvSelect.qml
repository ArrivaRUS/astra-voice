// Выпадающий список — design/spec.md §4.4.
// Закрытый: 33,5 высоты, паддинг 6/10, шеврон chevd 13. Раскрытый: не более 6 пунктов (238 px),
// вниз, а если места нет — вверх; выбранный пункт — selection-bg / selection-fg.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

ComboBox {
    id: control

    padding: 0
    leftPadding: Theme.selectPaddingX
    rightPadding: Theme.selectPaddingX
    implicitHeight: Theme.selectHeight
    font.family: Theme.fontUi
    font.pixelSize: Theme.fontSelectSize

    contentItem: Text {
        text: control.displayText
        font: control.font
        renderType: Text.NativeRendering
        color: control.enabled ? Theme.fg : Theme.fgDisabled
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
        rightPadding: Theme.selectChevron + Theme.selectGap
    }

    indicator: Icon {
        name: "chevd"
        size: Theme.selectChevron
        color: control.enabled ? Theme.fgMuted : Theme.fgDisabled
        x: control.width - width - Theme.selectPaddingX
        y: (control.height - height) / 2
    }

    background: Rectangle {
        radius: Theme.selectRadius
        color: control.enabled ? Theme.bgSurface : Theme.bgSurface2
        border.width: Theme.fieldBorder
        border.color: control.popup.visible ? Theme.selectOpenState : Theme.border

        Rectangle {
            anchors.fill: parent
            anchors.margins: -(Theme.focusOffset + Theme.focusWidth)
            radius: Theme.focusRadius
            color: "transparent"
            border.width: Theme.focusWidth
            border.color: Theme.stateFocusRing
            visible: control.visualFocus
        }
    }

    delegate: ItemDelegate {
        required property int index
        required property string modelData

        width: control.popup.width - Theme.popoverPadding * 2
        height: Theme.popoverItemH
        padding: 0
        leftPadding: Theme.popoverItemPaddingX
        rightPadding: Theme.popoverItemPaddingX

        contentItem: Text {
            text: parent.modelData
            font.family: Theme.fontUi
            font.pixelSize: Theme.fontMenuItemSize
            font.weight: parent.index === control.currentIndex ? Font.Medium : Font.Normal
            renderType: Text.NativeRendering
            color: parent.index === control.currentIndex ? Theme.selectionFg : Theme.fg
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }

        background: Rectangle {
            radius: Theme.popoverItemRadius
            color: parent.index === control.currentIndex ? Theme.selectionBg
                 : (parent.hovered ? Theme.stateHoverOnSurface : "transparent")
        }
    }

    popup: Popup {
        y: control.height
        width: control.width
        implicitHeight: Math.min(listView.contentHeight + Theme.popoverPadding * 2, Theme.popoverMaxH)
        padding: Theme.popoverPadding

        contentItem: ListView {
            id: listView
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            boundsBehavior: Flickable.StopAtBounds

            ScrollBar.vertical: ScrollBar {
                id: listScroll
                policy: ScrollBar.AsNeeded
                visible: size < 1.0
                width: Theme.scrollbarW

                contentItem: Rectangle {
                    implicitWidth: Theme.scrollbarW
                    radius: Theme.scrollbarRadius
                    color: Theme.scrollbarColor
                    opacity: listScroll.active ? Theme.scrollbarOpacity : 0

                    Behavior on opacity {
                        NumberAnimation {
                            duration: Theme.durationExit
                            easing.type: Easing.Bezier
                            easing.bezierCurve: Theme.easingExit.concat([1, 1])
                        }
                    }
                }
            }
        }

        background: Rectangle {
            radius: Theme.popoverRadius
            color: Theme.bgSurface
            border.width: Theme.fieldBorder
            border.color: Theme.border
        }
    }
}
