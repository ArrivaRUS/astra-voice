Лицензии компонентов AppImage Astra Voice
==========================================

LICENSE и NOTICE уровнем выше — лицензия и уведомления самой программы.
Машиночитаемый состав — sbom-appimage.cdx.json (CycloneDX), публикуется рядом с образом.

licenses/common/      полные тексты GPL-2.0, GPL-3.0, LGPL-2.1, LGPL-3.0, Apache-2.0, MPL-2.0
                      (на них ссылаются остальные файлы; на компьютере пользователя
                      /usr/share/common-licenses может отсутствовать)
licenses/debian/      copyright пакетов Debian 12, из которых взят интерпретатор Python 3.11
licenses/wheels/      файлы лицензий из колёс Python (dist-info и пакетов)
licenses/PyQt5/       лицензия PyQt5 (GPL v3) — в колесе PyQt5 её файла нет
licenses/type2-runtime/  лицензия загрузчика AppImage и состав вшитых в него библиотек
INDEX.txt             какой файл к какому компоненту относится

Qt 5.15.19 (qtbase, qtdeclarative, qtquickcontrols2, qtsvg) используется на условиях
GNU LGPL v3 (licenses/common/LGPL-3.0.txt, licenses/wheels/pyqt5_qt5-*/LICENSE).
Библиотеки Qt лежат отдельными файлами в opt/python3.11/lib/python3.11/site-packages/PyQt5/Qt5/
распакованной копии и могут быть заменены совместимой сборкой Qt 5.15.
Исходный код Qt, PyQt5, Python (Debian), загрузчика AppImage и libfuse опубликован
вместе с выпуском (архив исходников на странице Release).
