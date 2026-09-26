// Контурная иконка интерфейса; контуры хранятся в src/astra_voice/ui/icons.py.
import QtQuick 2.15
import QtQuick.Window 2.15

Item {
    id: root

    property string name: ""
    property real size: 16
    property color color: "#000000"
    property real strokeWidth: 1.5

    implicitWidth: size
    implicitHeight: size
    width: size
    height: size

    function hex(channel) {
        return ("0" + Math.round(channel * 255).toString(16)).slice(-2)
    }

    // .patches/019: Shape не подчиняется clip Flickable в программном рендере.
    // layer сдвигает и обрезает иконки на HiDPI; data: URI идёт через сетевой стек Qt
    // и нарушает ИБ-12. Контуры находятся в src/astra_voice/ui/icons.py.
    Image {
        anchors.fill: parent
        source: root.name === "" ? "" : "image://avicon/" + root.name + "?c="
                + root.hex(root.color.r) + root.hex(root.color.g)
                + root.hex(root.color.b) + root.hex(root.color.a)
                + "&w=" + root.strokeWidth
        sourceSize: Qt.size(Math.ceil(root.size * Screen.devicePixelRatio),
                            Math.ceil(root.size * Screen.devicePixelRatio))
        smooth: true
        mipmap: false
        asynchronous: false
        cache: true
        fillMode: Image.Stretch
    }
}
