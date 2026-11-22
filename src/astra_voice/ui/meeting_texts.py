"""Literal E13 interface copy from design/meetings/spec.md §3/§4.

No user data belongs in this vocabulary.
"""

from __future__ import annotations

RETENTION_TEXT = "Удалено встреч по сроку хранения: {count}"

CALL_LOST_TEXT = {
    "dion": (
        "Не удалось однозначно отделить звук DION — "
        "запишется только микрофон или весь звук компьютера"
    ),
    "browser": (
        "Не удалось однозначно отделить звук браузера — "
        "запишется только микрофон или весь звук компьютера"
    ),
}

W4_TEXTS = {
    "title": "Записать встречу",
    "heading": "Что записывать",
    "callTitle": "Звонок в DION",
    "callCaption": "Ваш микрофон и звук DION. Другие звуки компьютера в запись не попадут.",
    "browserTitle": "Звонок в браузере",
    "browserCaption": "Ваш микрофон и звук браузера — всех вкладок, не только вкладки звонка.",
    "roomTitle": "Встреча в комнате — только микрофон",
    "roomCaption": "Звук компьютера не записывается.",
    "allTitle": "Весь звук компьютера и микрофон",
    "allCaption": (
        "Для программы видеосвязи, которую Astra Voice не узнаёт. "
        "В запись попадут все звуки компьютера: уведомления, музыка, другие программы."
    ),
    "noAppSplit": (
        "На этом компьютере программа не умеет отделить звук видеосвязи — "
        "в запись попадёт весь звук компьютера."
    ),
    "microphone": "Микрофон",
    "speakers": (
        "В гарнитуре запись будет чище: без неё голоса собеседников попадут и в ваш микрофон."
    ),
    "reminder": "После начала записи появится текст, чтобы предупредить участников.",
    "cancel": "Отмена",
    "start": "Начать запись",
    "starting": "Начинаю…",
    "openMeetings": "Открыть встречи",
    "retry": "Повторить",
    "interruptedTitle": "Запись встречи прервалась",
    "close": "Закрыть",
}

TEXTS = {
    "title": "Встречи",
    "subtitle": "Запись встреч и расшифровка на этом компьютере",
    "offTitle": "Запись встреч выключена",
    "offDescription": (
        "Astra Voice может записать встречу — ваш микрофон и звук собеседников из программы "
        "видеосвязи — и сделать из записи текст прямо на этом компьютере, без интернета. "
        "Запись начинается, только когда вы нажмёте «Начать запись»."
    ),
    "enable": "Включить запись встреч",
    "offCaption": "Пока функция выключена, программа не следит за звонками и ничего не записывает.",
    "policyOff": "Запись встреч отключена администратором",
    "policyInvalid": "Значение политики некорректно — обратитесь к администратору",
    "policyNote": (
        "Часть настроек задана администратором вашей организации — "
        "они показаны серым и недоступны для изменения"
    ),
    "record": "Записать встречу",
    "recordingCaption": "Уже идёт запись встречи",
    "recordedHeading": "ЗАПИСАННЫЕ ВСТРЕЧИ",
    "empty": (
        "Встреч пока нет. Нажмите «Записать встречу» или выберите этот пункт в меню значка в трее."
    ),
    "error": "Не удалось прочитать папку встреч. Проверьте, что диск подключён и доступен.",
    "retry": "Повторить",
    "recordHeading": "ЗАПИСЬ",
    "offer": "Предлагать запись, когда начинается звонок",
    "offerCaption": "Программа заметит звонок и спросит. Сама запись не начнётся",
    "knownApps": "Какие звонки узнаются",
    "noticeHeading": "ПРЕДУПРЕЖДЕНИЕ УЧАСТНИКОВ",
    "noticeLabel": "Текст для чата встречи",
    "noticeCaption": "Показывается при каждом начале записи — с кнопкой «Скопировать для чата»",
    "inviteLabel": "Текст для приглашения на встречу",
    "inviteCaption": "Предупредить заранее надёжнее всего — добавьте этот текст в приглашение",
    "copy": "Скопировать",
    "copied": "✓ Скопировано",
    "transcriptionHeading": "РАСШИФРОВКА",
    "modelLabel": "Модель для расшифровки",
    "modelMissing": "Не установлена — без неё расшифровки не будет",
    "install": "Установить",
    "transcribeImmediately": "Расшифровывать сразу после записи",
    "diarization": "Разделять собеседников по голосам",
    "diarizationCaption": (
        "Собеседники станут «Спикер 1, 2…». "
        "Голоса не запоминаются — ни между встречами, ни внутри встречи"
    ),
    "selfLabel": "Как подписывать вас в расшифровке",
    "retentionHeading": "ХРАНЕНИЕ",
    "audioRetention": "Хранить запись звука после расшифровки",
    "audioRetentionCaption": (
        "Расшифровка остаётся. Если расшифровать не удалось, звук не удаляется"
    ),
    "meetingRetention": "Удалять встречи старше",
    "privateCaption": (
        "Встречи хранятся только на этом компьютере и доступны только вашей учётной записи"
    ),
    "aiHeading": "ШАБЛОНЫ С ИИ",
    "aiDescription": (
        "Краткое содержание и протокол встречи делает языковая модель на сервере, адрес "
        "которого вы укажете. Текст встречи уходит туда только по вашему нажатию. "
        "Пока адреса нет, этих шаблонов нет."
    ),
    "aiEndpoint": "Адрес сервиса ИИ",
    "aiModel": "Модель",
    "aiKey": "Ключ доступа",
    "aiKeyOptional": "необязательно",
    "aiKeyCaption": "Хранится в системном хранилище паролей",
    "aiCheck": "Проверить",
    "aiCheckCaption": "Отправит пробный запрос без текста встреч",
    "aiOff": "Шаблоны с ИИ отключены администратором",
    "aiAdminAddress": "Адрес задан администратором, модель и ключ — ваши",
    "disable": "Выключить запись встреч",
    "disableCaption": "Сохранённые встречи останутся. Удалить их можно в списке выше",
    "disableRecordingCaption": "Сначала остановите запись встречи",
    "d1Title": "Запись встреч",
    "d1Heading": "Перед тем как включить",
    "d1Personal": "На рабочем компьютере действуют правила вашей организации о записи совещаний.",
    "d1Organization": (
        "Этим компьютером управляет ваша организация. "
        "Записывайте совещания по её правилам — их можно узнать у администратора."
    ),
    "d1OfferCaption": "Программа только спросит — запись сама не начнётся",
    "d1ManualCaption": "Недоступно — так настроено администратором вашей организации",
    "cancel": "Отмена",
    "accept": "Понятно, включить",
}

RESPONSIBILITY_ITEMS = (
    {
        "title": "Что записывается",
        "text": (
            "Ваш микрофон и звук собеседников из программы видеосвязи. Из записи "
            "программа делает текст — на этом компьютере, без интернета."
        ),
        "icon": "meeting",
        "warning": False,
    },
    {
        "title": "Когда",
        "text": (
            "Только после того, как вы нажмёте «Начать запись». Пока идёт запись, на "
            "экране видна плашка с таймером — скрыть её нельзя, остановить можно одним "
            "нажатием."
        ),
        "icon": "rec",
        "warning": False,
    },
    {
        "title": "Где хранится",
        "text": (
            "Только на этом компьютере. Звук удаляется сразу после расшифровки, если вы "
            "не выберете иначе. Встречу можно удалить целиком в любой момент."
        ),
        "icon": "home",
        "warning": False,
    },
    {
        "title": "Предупреждать участников — ваша обязанность",
        "text": (
            "При каждом начале записи программа покажет готовый текст для чата встречи — "
            "скопируйте его и скажите вслух. Лучше предупреждать заранее — например, в "
            "приглашении на встречу."
        ),
        "icon": "alert",
        "warning": True,
    },
    {
        "title": "Только свои встречи",
        "text": (
            "Записывайте только встречи, в которых участвуете сами, и не передавайте "
            "запись другим без согласия участников."
        ),
        "icon": "people",
        "warning": False,
    },
)

W2_TEXTS = {
    "recording": "Запись встречи",
    "paused": "Пауза",
    "pause": "Пауза",
    "resume": "Продолжить",
    "stop": "Остановить",
    "participants": "Текст для участников",
    "collapse": "Свернуть плашку",
    "expand": "Развернуть плашку",
    "keyboard": "Управлять плашкой с клавиатуры…",
    "grip": "Переместить плашку",
    "stopping": "Сохраняю запись…",
    "pausedLock": "Экран был заблокирован — продолжите вручную",
    "pausedUnknown": "Состояние сеанса неизвестно — продолжите вручную",
    "micPaused": "Ваш голос не записывается: микрофон сменился",
    "micName": "Ваш голос не записывается: микрофон сменился на «{name}»",
    "resumeMicrophone": "Продолжить с «{name}»",
    "resumeOtherMicrophone": "Продолжить с другим микрофоном",
    "appClosed": "DION закрылся — пишется только микрофон",
    "farEnded": "Звук собеседников больше не записывается — пишется только микрофон",
    "farPartial": "Часть звука не записывается",
    "limitSoon": "Остановится через {remaining}",
    "extend": "Продлить на час",
    "pausedTimer": "На паузе {minutes} мин. Через 60 мин паузы встреча сохранится",
    "restoredIndicator": (
        "Запись стояла на паузе {duration}: индикатор записи не отображался. "
        "Отметка — в расшифровке"
    ),
    "restoredHang": (
        "Запись стояла на паузе {duration}: программа не отвечала. Отметка — в расшифровке"
    ),
    "audioError": "Запись остановлена: звуковая служба не отвечает. Записанное сохранено",
    "trayRecording": "Идёт запись встречи",
    "trayPaused": "Запись встречи на паузе",
    "trayLimit": "остановится через {remaining}",
    "trayStop": "Остановить запись встречи",
    "trayParticipants": "Текст для участников…",
    "trayRecord": "Записать встречу",
    "trayMeetings": "Встречи…",
}

# W1, spec §4.3. Only these fixed strings go to the offer window.
W1_TEXTS = {
    "header": "Astra Voice",
    "start": "Начать запись",
    "refuse": "Не записывать",
    "close": "Закрыть",
}
W1_REMAINING = "Закроется через {seconds} с"
_W1_BODY = "Запись начнётся с момента нажатия — сказанное раньше в неё не попадёт."
W1_CALL_TEXTS = {
    "call-app": {
        "title": "Идёт звонок в DION. Начать запись встречи?",
        "body": _W1_BODY,
    },
    "call-browser": {
        "title": "Идёт звонок в браузере. Начать запись встречи?",
        "body": _W1_BODY + " Запишется звук браузера целиком — всех вкладок.",
    },
}

# W3 «Предупредите участников о записи», spec §3.6/§4.6. The notice text itself
# is the effective `meeting_notice_text` (user or administrator), never a constant here.
W3_TEXTS = {
    "header": "Astra Voice",
    "title": "Предупредите участников о записи",
    "copy": "Скопировать для чата",
    "copied": "Скопировано",
    "copyFailed": "Не удалось скопировать — выделите текст и скопируйте вручную",
    "hint": "И скажите это вслух в начале встречи.",
    "admin": "Текст задан администратором",
    "close": "Закрыть",
}
# MN-17 (T-444): the bottom line is always shown. Spec names DION; any other
# source has no verified program name, so the line stays generic.
W3_MUTE = (
    "Ваш микрофон записывается всё время записи — выключенный микрофон в {app} на запись не "
    "влияет. Чтобы не записывать, нажмите «Пауза»."
)
W3_APP_DION = "DION"
W3_APP_OTHER = "программе видеосвязи"


def notice_mute_text(app_class: str) -> str:
    return W3_MUTE.format(app=W3_APP_DION if app_class == "dion" else W3_APP_OTHER)


# Exit during a recording: spec §4.6, dialog §11.1, mockup 04-recording section 7.
EXIT_TEXTS = {
    "header": "Astra Voice",
    "title": "Идёт запись встречи. Остановить запись и выйти?",
    "text": "Записанное сохранится и будет расшифровано при следующем запуске.",
    "cancel": "Отмена",
    "confirm": "Остановить и выйти",
}


# D2, spec §4.7; sent-to-AI notice belongs to M9.
DELETE_TEXTS = {
    "heading": "Удалить встречу",
    "prefix": "Удалить встречу «",
    "suffix": "»?",
    "text": "Удалятся запись звука, расшифровка и все её файлы в программе. "
    "Файлы, которые вы сохранили в другие папки, останутся — удалите их сами.",
    "cancel": "Отмена",
    "confirm": "Удалить встречу",
    "error": "Не удалось удалить встречу",
}


# D3, spec §4.7; meeting titles are supplied separately by the bridge.
RENAME_TEXTS = {
    "heading": "Переименовать встречу",
    "label": "Название встречи",
    "cancel": "Отмена",
    "confirm": "Сохранить",
}


# Reply marks, spec §Д.4/Д.8. Only constants define marks; meeting text is data.
MARK_TEXTS = {
    "echo": "возможное эхо",
    "multi": "несколько говорящих",
    "badge": "Возможное эхо",
    "excluded": "Возможное эхо не включено в файл",
    "statistics": "Не учтено в статистике: возможное эхо — {minutes} мин · {replies}",
    "excludeEcho": "Не включать реплики с пометкой «возможное эхо»",
    "excludeEchoDescription": (
        "Похожи на голоса собеседников из колонок, но среди них может быть и ваша речь."
    ),
}


def reply_count(count: int) -> str:
    noun = (
        "реплик"
        if 11 <= count % 100 <= 14
        else {1: "реплика", 2: "реплики", 3: "реплики", 4: "реплики"}.get(count % 10, "реплик")
    )
    return f"{count} {noun}"


def echo_label(count: int, *, excluded: bool = False, live: bool = False) -> str:
    label = MARK_TEXTS["excluded" if excluded else "badge"]
    return label if live else f"{label}: {reply_count(count)}"


# spec §У.3–У.5. Keep both surfaces on one vocabulary and one state.
DIAR_INSTALL_TEXTS = {
    "title": "Модели для разделения по голосам",
    "missing": "Не установлены — без них все собеседники подписываются «Собеседники» · 34 МБ",
    "verifying": "Проверяю модели…",
    "installing": "Устанавливаю модели…",
    "installed": "Установлены · 34 МБ",
    "cancelled": "Установка отменена",
    "broken": "Не удалось установить модели разделения. Попробуйте ещё раз.",
    "busy": "Уже выполняется установка или удаление моделей разделения. Попробуйте позже.",
    "damaged": "Файлы моделей не читаются",
    "offline-user": "Не установлены · Включён офлайн-режим — «Сеть и обновления»",
    "policy": "Не установлены · Задано администратором: работа без сети",
    "cardMissing": "Разделение по голосам: модели не установлены",
    "cardInstalled": "Модели установлены — следующие расшифровки будут разделены по голосам",
    "prefix": "Разделение по голосам: ",
    "install": "Установить",
    "cancel": "Отмена",
    "retry": "Повторить",
    "reinstall": "Переустановить",
    "installName": "Установить модели для разделения по голосам, 34 МБ",
    "cancelName": "Отменить установку моделей разделения",
    "retryName": "Повторить установку моделей разделения",
    "reinstallName": "Переустановить модели для разделения по голосам",
    "progressName": "Установка моделей разделения",
}


def diar_install_presentation(
    code: str, percent: int, source: str, *, reason_code: str = "", busy: bool = False
) -> dict[str, str | int | bool]:
    """Closed UI vocabulary; service exception/reason strings never reach QML."""
    from astra_voice.ui.model_downloads import (
        _FAILURE_MESSAGES,
        _REVISION_FAILURE_REASONS,
        _SOURCE_TEXT,
    )

    texts = DIAR_INSTALL_TEXTS
    revoked = reason_code == "revoked"
    if code == "downloading":
        text = f"{_SOURCE_TEXT.get(source, _SOURCE_TEXT['hf'])} · {percent} %"
    elif revoked:
        reasons = _REVISION_FAILURE_REASONS["revoked"]
        assert isinstance(reasons, dict)
        text = reasons["update"]
    elif code.startswith("failed:") or code == "no-space":
        failure = code.partition(":")[2] if code.startswith("failed:") else code
        text = _FAILURE_MESSAGES.get(failure, _FAILURE_MESSAGES["server"]).replace(
            "модель", "модели"
        )
    else:
        text = texts["busy" if code == "broken" and busy else code]
    action = (
        ""
        if revoked or code in {"installed", "offline-user", "policy"}
        else "cancel"
        if code in {"downloading", "verifying", "installing"}
        else "install"
        if code in {"missing", "cancelled"}
        else "reinstall"
        if code == "damaged"
        else "retry"
    )
    card_text = (
        texts["cardInstalled"]
        if code == "installed"
        else texts["cardMissing"]
        if code in {"missing", "offline-user", "policy"}
        else texts["prefix"] + text[0].lower() + text[1:]
    )
    return {
        "code": code,
        "percent": percent,
        "source": source,
        "text": text,
        "action": texts[action] if action else "",
        "actionName": texts[action + "Name"] if action else "",
        "cardText": card_text,
        "title": texts["title"],
        "progressName": texts["progressName"],
        "error": revoked
        or code == "broken"
        or code == "no-space"
        or code == "damaged"
        or code.startswith("failed:"),
    }


# «Разделить заново», spec §Р.2–Р.6. Only closed codes enter this vocabulary.
REDO_TEXTS = {
    "prefix": "Разделение по голосам: ",
    "failed": "не удалось — {labels}. Звук сохранён, можно попробовать ещё раз",
    "queued": "начнётся после текущей расшифровки",
    "wait-memory": "подождёт — сейчас мало свободной памяти",
    "running": "разделяю заново · {percent} %",
    "running-first": "разделяю · {percent} %",
    "reconcile": "уточняю реплики · {percent} %",
    "cancelled": "отменено — {labels}",
    "cancelled-named": "отменено — оставлены прежнее разделение и имена",
    "failed-again": "снова не удалось — {labels}. Звук сохранён, можно повторить позже",
    "no-audio": "не удалось — {labels}. Записи звука нет, разделить заново нельзя",
    "done": "Собеседники разделены по голосам — можно вписать имена участников",
    "redo-failed": "разделить заново не удалось — оставлены прежнее разделение и имена",
    "offer": "эту встречу можно разделить, пока хранится звук (до {date})",
    "offer-installed": "Модели установлены — эту встречу тоже можно разделить по голосам",
    "kept-deleted": "оставлено как есть. Звук удалён по настройке хранения",
    "kept-retained": (
        "оставлено как есть. Звук хранится до {date} — до этого встречу можно разделить"
    ),
    "kept-pending": "оставлено как есть. Звук удаляется по настройке хранения",
    "recovery_error": "не удалось завершить — программа повторит попытку сама",
    "readError": "Не удалось прочитать расшифровку. Попробуйте ещё раз.",
    "again": "Разделить заново",
    "divide": "Разделить по голосам",
    "cancel": "Отменить",
    "keep": "Оставить так",
    "againName": "Разделить собеседников по голосам заново",
    "divideName": "Разделить собеседников этой встречи по голосам",
    "cancelName": "Отменить разделение по голосам",
    "keepName": "Оставить без разделения по голосам",
    "keepDeleteDescription": "Звук удалится сразу, разделить заново будет нельзя",
    "keepRetainDescription": "Звук будет храниться до {date}",
    "divideDescription": "Звук хранится до {date}",
    "namesLater": "Имена можно вписать после разделения по голосам.",
    "progressName": "Разделение по голосам",
    "badge": "Разделение по голосам · {percent} %",
    "warningHeading": "Разделить заново",
    "warningTitle": "Разделить встречу «{title}» по голосам заново?",
    "warningText": (
        "Имена участников и объединения «Это один человек» сбросятся: номера спикеров "
        "могут поменяться, поэтому имена не переносятся сами."
    ),
    "warningNames": " Сейчас заданы: {names}.",
    "warningCaption": (
        "Пока идёт разделение, встречу можно читать и сохранять. Если отменить или "
        "разделить не получится — всё останется как сейчас."
    ),
    "warningCancel": "Отмена",
    "exit": "Разделение продолжится; если разделение не успело — начнётся с начала",
}


def redo_presentation(
    code: str,
    percent: int,
    *,
    room: bool = False,
    first: bool = False,
    date: str = "",
    installed: bool = False,
    separated: bool = False,
) -> dict[str, str | int | bool]:
    """Literal row and accessibility text, independent of transient worker replies."""
    key = code
    if code == "running" and first:
        key = "running-first"
    elif code == "offer" and installed:
        key = "offer-installed"
    elif code == "cancelled" and separated:
        key = "cancelled-named"
    elif code in {"kept-7", "kept-30", "kept-retained"}:
        key = "kept-retained"
    labels = "все реплики подписаны «Участники»" if room else "собеседники подписаны «Собеседники»"
    text = REDO_TEXTS.get(key, "").format(labels=labels, percent=percent, date=date)
    if text and key not in {"done", "offer-installed"}:
        text = REDO_TEXTS["prefix"] + text
    error = code in {"failed", "failed-again", "redo-failed", "recovery_error"}
    success = code == "done" or code.startswith("kept-") or (code == "offer" and installed)
    return {
        "code": code,
        "percent": percent,
        "text": text,
        "visible": bool(text),
        "tone": "danger" if error else "muted",
        "icon": "alert" if error else "check" if success else "",
        "iconTone": "danger" if error else "success" if success else "muted",
        "showProgress": code in {"running", "reconcile"},
        "progressName": REDO_TEXTS["progressName"],
    }
