// S1 · QML-вариант пилюли (прототип qml/Pill.qml).
// НЕ ПРОГНАН: на стоковой ALSE 1.8.5 нет python3-pyqt5.qtquick (plan-claude §12) —
// зависимость ставит .deb в M1, после чего этот файл проверяется первым.
// Значения приходят из Python как contextProperty `tokens` (см. pill.py: load_tokens()),
// то есть источник тот же — design/tokens.json. Хардкода чисел здесь нет.
import QtQuick 2.15
import QtQuick.Window 2.15

Window {
    id: root
    // spec §8.1 — окно ОС, не элемент главного окна
    flags: Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint
           | Qt.WindowDoesNotAcceptFocus
    color: "transparent"
    visible: false            // показ — из Python, ПОСЛЕ выставления EWMH-атомов
    title: "astra-voice-pill"

    property var t: tokens
    property string pillState: pillModel.state
    property string pillText: pillModel.text
    property var levels: pillModel.levels

    // резиновая ширина 172…320 по TextMetrics, ellipsis запрещён (spec §8.2)
    TextMetrics {
        id: metrics
        font.family: t.font_family
        font.pixelSize: Math.round(t.font_px)
        text: root.pillText
    }
    readonly property int leadW: pillState === "listening" ? t.bars_block_w
                               : pillState === "processing"
                                 ? t.dots_count * t.dots_w + (t.dots_count - 1) * t.bar_gap
                                 : t.icon_16
    readonly property int trailW: pillState === "listening" ? t.gap + t.close_size
                                : pillState === "error" ? t.gap + 8 : 0
    readonly property int naturalW: t.pad_x + leadW + t.gap + Math.ceil(metrics.width)
                                    + trailW + t.pad_x

    width: Math.max(t.min_w, Math.min(t.max_w, naturalW))
    height: t.h

    Rectangle {
        id: body
        anchors.fill: parent
        radius: t.radius
        color: t.c_bg               // rgba(14,23,41,.94) — палитра пилюли с темой НЕ меняется
    }

    Row {
        anchors.verticalCenter: parent.verticalCenter
        anchors.horizontalCenter: parent.horizontalCenter
        spacing: t.gap

        // 9 столбиков уровня (spec §8.3): h = max(3, round(level*20))
        Row {
            visible: root.pillState === "listening"
            spacing: t.bar_gap
            height: t.bar_area_h
            Repeater {
                model: t.bars
                Rectangle {
                    width: t.bar_w
                    radius: t.bar_radius
                    color: t.c_bar_live
                    height: Math.max(t.bar_h_min,
                                     Math.round((root.levels[index] || 0) * t.bar_h_max))
                    anchors.bottom: parent.bottom
                }
            }
        }

        // 3 точки «распознаю», пульсация (spec §8.4 п.6)
        Row {
            id: dots
            visible: root.pillState === "processing"
            spacing: t.bar_gap
            Repeater {
                model: t.dots_count
                Rectangle {
                    width: t.dots_w
                    radius: t.dots_w / 2
                    color: t.c_processing
                    height: t.dots_h[index]
                    anchors.verticalCenter: parent.verticalCenter
                    SequentialAnimation on scale {
                        running: dots.visible
                        loops: Animation.Infinite
                        NumberAnimation { to: 1.0; duration: 300 + index * 90 }
                        NumberAnimation { to: 0.65; duration: 300 + index * 90 }
                    }
                }
            }
        }

        // check / alert — глифы набора 16×16, обводка 1.5 (spec §8.4)
        Canvas {
            visible: root.pillState === "done" || root.pillState === "error"
            width: t.icon_16; height: t.icon_16
            anchors.verticalCenter: parent.verticalCenter
            onPaint: {
                var ctx = getContext("2d");
                ctx.reset();
                ctx.lineWidth = t.icon_stroke;
                ctx.lineCap = "round";
                ctx.lineJoin = "round";
                ctx.strokeStyle = root.pillState === "done" ? t.c_done : t.c_error;
                ctx.beginPath();
                if (root.pillState === "done") {
                    ctx.moveTo(3.5, 8.5); ctx.lineTo(6.5, 11.5); ctx.lineTo(12.5, 4.5);
                } else {
                    ctx.moveTo(8, 2); ctx.lineTo(15, 13.5); ctx.lineTo(1, 13.5);
                    ctx.closePath();
                    ctx.moveTo(8, 6.5); ctx.lineTo(8, 9.5);
                }
                ctx.stroke();
            }
            Connections { target: root; function onPillStateChanged() { parent.requestPaint() } }
        }

        Text {
            text: root.pillText
            color: root.pillState === "error" ? t.c_error : t.c_fg
            font.family: t.font_family
            font.pixelSize: Math.round(t.font_px)
            elide: Text.ElideNone          // ellipsis запрещён (G2)
            anchors.verticalCenter: parent.verticalCenter
        }

        // «×» — только в listening (spec §8.4 п.3)
        Rectangle {
            visible: root.pillState === "listening"
            width: t.close_size; height: t.close_size; radius: width / 2
            color: t.c_close_bg
            anchors.verticalCenter: parent.verticalCenter
            Canvas {
                anchors.fill: parent
                onPaint: {
                    var ctx = getContext("2d");
                    ctx.reset();
                    ctx.lineWidth = t.icon_stroke;
                    ctx.lineCap = "round";
                    ctx.strokeStyle = t.c_close_fg;
                    var c = width / 2, r = 4;
                    ctx.beginPath();
                    ctx.moveTo(c - r, c - r); ctx.lineTo(c + r, c + r);
                    ctx.moveTo(c + r, c - r); ctx.lineTo(c - r, c + r);
                    ctx.stroke();
                }
            }
        }

        Text {
            visible: root.pillState === "error"
            text: "›"
            color: t.c_error
            font.family: t.font_family
            font.pixelSize: Math.round(t.font_px)
            anchors.verticalCenter: parent.verticalCenter
        }
    }
}
