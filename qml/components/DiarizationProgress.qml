// Qt Controls supplies QAccessibleValueInterface; visual and announced progress differ.
import QtQuick 2.15
import QtQuick.Controls 2.15
import ".."

ProgressBar {
    id: root
    property var install: ({})
    from: 0
    to: 100
    value: install.accessiblePercent || 0
    padding: 0
    height: Theme.progressHThin
    implicitHeight: Theme.progressHThin
    Accessible.role: Accessible.ProgressBar
    Accessible.name: install.progressName || ""
    Accessible.ignored: !visible
    background: Rectangle { color: Theme.bgSurface2; radius: Theme.progressRadius }
    contentItem: Item {
        Rectangle {
            height: parent.height
            width: parent.width * (root.install.code === "verifying" || root.install.code === "installing" ? 1 : (root.install.percent || 0) / 100)
            radius: Theme.progressRadius
            color: Theme.primary
        }
    }
}
