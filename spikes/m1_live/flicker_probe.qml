// Минимальный пробник «моргания» для живого прогона (spikes/m1_live/flicker.md).
// Тот же механизм hover, что в qml/components/SidebarItem.qml: MouseArea.hoverEnabled +
// Behavior on color (120 мс), но БЕЗ остального приложения. Если моргает и здесь —
// причина в платформе (KWin X11 / Mesa / панель), а не в коде Astra Voice.
//
// Запуск (в сессии заказчика, ≤ 30 с):
//   QT_SELECT=qt5 qmlscene spikes/m1_live/flicker_probe.qml
//   QSG_RENDER_LOOP=basic      QT_SELECT=qt5 qmlscene spikes/m1_live/flicker_probe.qml
//   QT_QUICK_BACKEND=software  QT_SELECT=qt5 qmlscene spikes/m1_live/flicker_probe.qml
import QtQuick 2.15
import QtQuick.Window 2.15

Window {
    width: 900
    height: 588
    visible: true
    color: "#F7F9FC"
    title: "flicker-probe"

    Column {
        x: 10
        y: 60
        width: 164
        spacing: 2

        Repeater {
            model: 6

            Rectangle {
                width: parent.width
                height: 34
                radius: 7
                color: index === 0 ? "#1B3A73" : (area.containsMouse ? "#E7ECF4" : "transparent")

                Behavior on color { ColorAnimation { duration: 120 } }

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    x: 12
                    text: "Пункт " + (index + 1)
                    color: index === 0 ? "white" : "#3A4560"
                    renderType: Text.NativeRendering
                }

                MouseArea {
                    id: area
                    anchors.fill: parent
                    hoverEnabled: true
                }
            }
        }
    }

    Text {
        x: 220
        y: 60
        width: 640
        wrapMode: Text.WordWrap
        color: "#3A4560"
        renderType: Text.NativeRendering
        text: "Водите мышью по пунктам слева. Моргает экран/окно? — запишите: да/нет, "
              + "и для какого запуска (обычный / QSG_RENDER_LOOP=basic / QT_QUICK_BACKEND=software)."
    }
}
