/* Video Factory — translations. No framework, no build step.
   Three languages because the target user is explicitly "even grandma" —
   plain language matters more here than anywhere else in the app. */

const I18N = {
  uz: {
    "app.tagline": "Suratdan video yasang",
    "nav.create": "Yaratish",
    "nav.history": "Videolarim",

    "auth.signin": "Kirish",
    "auth.signup": "Ro'yxatdan o'tish",
    "auth.signout": "Chiqish",
    "auth.title.signin": "Kirish",
    "auth.title.signup": "Ro'yxatdan o'tish",
    "auth.subtitle": "Video yaratish uchun hisobingiz kerak.",
    "auth.email": "Email",
    "auth.password": "Parol",
    "auth.needAccount": "Hisobingiz yo'qmi? Ro'yxatdan o'ting",
    "auth.haveAccount": "Hisobingiz bormi? Kiring",
    "auth.google": "Google orqali kirish",
    "auth.or": "yoki",
    "auth.checkEmail": "Hisob yaratildi. Kirishdan oldin emailingizni tasdiqlang.",
    "auth.wrongPassword": "Email yoki parol noto'g'ri.",

    "step1.badge": "1",
    "step1.title": "Nima haqida video?",
    "step1.subtitle": "Ko'rmoqchi bo'lgan manzarani so'zlar bilan tasvirlab bering",
    "step1.placeholder": "Masalan: quyoshli tongda kofe tayyorlayotgan barista, issiq yorug'lik",
    "step1.button": "Suratlarni yaratish",
    "step1.hint": "Avval bir nechta surat yaratiladi — eng yaxshisini o'zingiz tanlaysiz",
    "step1.presets": "Tayyor g'oyalar",

    "step2.title": "Birini tanlang",
    "step2.subtitle": "Video aynan shu suratdan jonlanadi",
    "step2.choose": "Shuni tanlash",
    "step2.chosen": "Tanlandi",
    "step2.empty.title": "Hali surat yo'q",
    "step2.empty.body": "Yuqorida video haqida yozib, yaratishni bosing",

    "step3.title": "Endi jonlantiramiz",
    "step3.subtitle": "Suratda nima harakat qilishini yozing (ixtiyoriy)",
    "step3.placeholder": "Masalan: kamera sekin yaqinlashadi, bug ko'tariladi",
    "step3.duration": "Davomiyligi",
    "step3.duration.short": "Qisqa",
    "step3.duration.medium": "O'rta",
    "step3.duration.long": "Uzun",
    "step3.button": "Video yaratish",
    "step3.gate": "Avval 2-qadamda suratni tanlang",
    "step3.ready": "Surat tanlandi. Video yaratishga tayyor.",

    "step4.title": "Videongiz tayyor",
    "step4.subtitle": "Ko'ring, yuklab oling yoki yaxshilang",
    "step4.download": "Yuklab olish",
    "step4.enhance": "Yaxshilash",
    "step4.empty.title": "Hali video yo'q",
    "step4.empty.body": "Tayyor bo'lgan videongiz shu yerda ko'rinadi",

    "history.title": "Mening videolarim",
    "history.subtitle": "Barcha yaratganlaringiz, eng yangisidan boshlab",
    "history.signin.title": "Kirish kerak",
    "history.signin.body": "Videolaringiz faqat sizga ko'rinadi",
    "history.empty.title": "Hali hech narsa yo'q",
    "history.empty.body": "Yaratgan surat va videolaringiz shu yerda to'planadi",
    "history.failed": "Yuklanmadi",
    "history.badge.video": "Video",
    "history.badge.image": "Surat",

    "confirm.title": "Narxni tasdiqlang",
    "confirm.note": "Bu haqiqiy pul sarflaydi. Faqat muvaffaqiyatli bo'lsa yechiladi.",
    "confirm.cancel": "Bekor qilish",
    "confirm.ok": "Ha, boshlash",

    "postprod.title": "Videoni yaxshilash",
    "postprod.op": "Nima qilamiz?",
    "postprod.op.upscale": "Sifatni oshirish",
    "postprod.op.bgremove": "Fonni olib tashlash",
    "postprod.op.subtitles": "Subtitr qo'shish",
    "postprod.op.lipsync": "Ovozni og'izga moslash",
    "postprod.cancel": "Bekor qilish",
    "postprod.getprice": "Narxni bilish",
    "postprod.upscale.quality": "Sifat darajasi",
    "postprod.upscale.q1": "Yaxshi",
    "postprod.upscale.q2": "Juda yaxshi",
    "postprod.upscale.q3": "Eng yuqori",
    "postprod.bgremove.color": "Fon rangi",
    "postprod.subtitles.lang": "Til (ixtiyoriy)",
    "postprod.subtitles.langPlaceholder": "Bo'sh qoldirsangiz o'zi aniqlaydi",
    "postprod.lipsync.audio": "Ovoz fayli havolasi",
    "postprod.lipsync.audioPlaceholder": "https://... ovoz faylining to'g'ridan-to'g'ri havolasi",
    "postprod.error.audioRequired": "Avval ovoz faylining havolasini kiriting.",

    "common.working": "Ishlanmoqda…",
    "common.workingHint": "Bu tabni yopmasangiz ham bo'ladi — jarayon davom etadi",
    "common.retry": "Qayta urinish",
    "common.errorTitle": "Xatolik yuz berdi",
    "common.connectError": "Serverga ulanib bo'lmadi. U ishlab turibdimi?",

    "health.falKeyMissing": "fal.ai kaliti sozlanmagan",
    "health.ffmpegMissing": "ffmpeg topilmadi",

    "toast.describeFirst": "Avval nima haqida ekanini yozing",
    "toast.pickFrame": "Avval 2-qadamda suratni tanlang",
    "toast.signInFirst": "Davom etish uchun kiring",
    "toast.videoReady": "Video tayyor!",
    "toast.enhanceReady": "Tayyor — \"Videolarim\"da ko'ring",
    "toast.enhanceFailed": "Yaxshilash amalga oshmadi",
    "toast.priceChanged": "Narx o'zgardi, qayta tasdiqlang",
    "toast.presetApplied": "qo'llanildi",
    "toast.signedIn": "Xush kelibsiz",
    "toast.starting": "Boshlanmoqda…",
    "toast.noImages": "Surat qaytmadi. Boshqacha yozib ko'ring.",
    "toast.noVideo": "Video qaytmadi.",

    "preset.product_reveal.name": "Mahsulot reklamasi",
    "preset.product_reveal.blurb": "Kamera sekin yaqinlashadi, yumshoq yorug'lik",
    "preset.talking_head.name": "Portret",
    "preset.talking_head.blurb": "Yuz aniq ko'rinadi, tabiiy harakat",
    "preset.cinematic_reveal.name": "Kinematik ochilish",
    "preset.cinematic_reveal.blurb": "Kamera pastdan yuqoriga, manzara ochiladi",
    "preset.action_beat.name": "Harakatli sahna",
    "preset.action_beat.blurb": "Tez, uzluksiz harakat",
    "preset.ambient_loop.name": "Fon uchun",
    "preset.ambient_loop.blurb": "Yumshoq, sokin harakat",
  },

  ru: {
    "app.tagline": "Создавайте видео из фото",
    "nav.create": "Создать",
    "nav.history": "Мои видео",

    "auth.signin": "Войти",
    "auth.signup": "Регистрация",
    "auth.signout": "Выйти",
    "auth.title.signin": "Вход",
    "auth.title.signup": "Регистрация",
    "auth.subtitle": "Для создания видео нужен аккаунт.",
    "auth.email": "Email",
    "auth.password": "Пароль",
    "auth.needAccount": "Нет аккаунта? Зарегистрируйтесь",
    "auth.haveAccount": "Уже есть аккаунт? Войдите",
    "auth.google": "Войти через Google",
    "auth.or": "или",
    "auth.checkEmail": "Аккаунт создан. Подтвердите email перед входом.",
    "auth.wrongPassword": "Неверный email или пароль.",

    "step1.badge": "1",
    "step1.title": "О чём видео?",
    "step1.subtitle": "Опишите словами, что хотите увидеть",
    "step1.placeholder": "Например: бариста готовит кофе солнечным утром, тёплый свет",
    "step1.button": "Создать фото",
    "step1.hint": "Сначала будет создано несколько фото — вы выберете лучшее",
    "step1.presets": "Готовые идеи",

    "step2.title": "Выберите одно",
    "step2.subtitle": "Видео оживит именно это фото",
    "step2.choose": "Выбрать это",
    "step2.chosen": "Выбрано",
    "step2.empty.title": "Пока нет фото",
    "step2.empty.body": "Опишите видео выше и нажмите «Создать»",

    "step3.title": "Теперь оживим",
    "step3.subtitle": "Опишите движение на фото (необязательно)",
    "step3.placeholder": "Например: камера медленно приближается, пар поднимается",
    "step3.duration": "Длительность",
    "step3.duration.short": "Коротко",
    "step3.duration.medium": "Средне",
    "step3.duration.long": "Долго",
    "step3.button": "Создать видео",
    "step3.gate": "Сначала выберите фото на шаге 2",
    "step3.ready": "Фото выбрано. Готово к созданию видео.",

    "step4.title": "Ваше видео готово",
    "step4.subtitle": "Смотрите, скачивайте или улучшайте",
    "step4.download": "Скачать",
    "step4.enhance": "Улучшить",
    "step4.empty.title": "Пока нет видео",
    "step4.empty.body": "Готовое видео появится здесь",

    "history.title": "Мои видео",
    "history.subtitle": "Всё, что вы создали, сначала новое",
    "history.signin.title": "Нужен вход",
    "history.signin.body": "Ваши видео видны только вам",
    "history.empty.title": "Пока ничего нет",
    "history.empty.body": "Созданные фото и видео появятся здесь",
    "history.failed": "Не загрузилось",
    "history.badge.video": "Видео",
    "history.badge.image": "Фото",

    "confirm.title": "Подтвердите цену",
    "confirm.note": "Это списывает реальные деньги. Только при успешном результате.",
    "confirm.cancel": "Отмена",
    "confirm.ok": "Да, начать",

    "postprod.title": "Улучшить видео",
    "postprod.op": "Что сделать?",
    "postprod.op.upscale": "Повысить качество",
    "postprod.op.bgremove": "Убрать фон",
    "postprod.op.subtitles": "Добавить субтитры",
    "postprod.op.lipsync": "Синхронизировать звук с губами",
    "postprod.cancel": "Отмена",
    "postprod.getprice": "Узнать цену",
    "postprod.upscale.quality": "Уровень качества",
    "postprod.upscale.q1": "Хорошо",
    "postprod.upscale.q2": "Очень хорошо",
    "postprod.upscale.q3": "Максимум",
    "postprod.bgremove.color": "Цвет фона",
    "postprod.subtitles.lang": "Язык (необязательно)",
    "postprod.subtitles.langPlaceholder": "Оставьте пустым для автоопределения",
    "postprod.lipsync.audio": "Ссылка на аудиофайл",
    "postprod.lipsync.audioPlaceholder": "https://... прямая ссылка на аудио",
    "postprod.error.audioRequired": "Сначала укажите ссылку на аудиофайл.",

    "common.working": "Обрабатывается…",
    "common.workingHint": "Можно не закрывать вкладку — процесс продолжится",
    "common.retry": "Повторить",
    "common.errorTitle": "Произошла ошибка",
    "common.connectError": "Не удалось подключиться к серверу. Он запущен?",

    "health.falKeyMissing": "ключ fal.ai не настроен",
    "health.ffmpegMissing": "ffmpeg не найден",

    "toast.describeFirst": "Сначала опишите, о чём видео",
    "toast.pickFrame": "Сначала выберите фото на шаге 2",
    "toast.signInFirst": "Войдите, чтобы продолжить",
    "toast.videoReady": "Видео готово!",
    "toast.enhanceReady": "Готово — смотрите в «Мои видео»",
    "toast.enhanceFailed": "Улучшение не удалось",
    "toast.priceChanged": "Цена изменилась, подтвердите ещё раз",
    "toast.presetApplied": "применено",
    "toast.signedIn": "Добро пожаловать",
    "toast.starting": "Запуск…",
    "toast.noImages": "Фото не получено. Попробуйте другое описание.",
    "toast.noVideo": "Видео не получено.",

    "preset.product_reveal.name": "Реклама продукта",
    "preset.product_reveal.blurb": "Камера медленно приближается, мягкий свет",
    "preset.talking_head.name": "Портрет",
    "preset.talking_head.blurb": "Лицо чётко видно, естественное движение",
    "preset.cinematic_reveal.name": "Кинематичное открытие",
    "preset.cinematic_reveal.blurb": "Камера снизу вверх, сцена раскрывается",
    "preset.action_beat.name": "Динамичная сцена",
    "preset.action_beat.blurb": "Быстрое, непрерывное движение",
    "preset.ambient_loop.name": "Для фона",
    "preset.ambient_loop.blurb": "Мягкое, спокойное движение",
  },

  en: {
    "app.tagline": "Turn a photo into video",
    "nav.create": "Create",
    "nav.history": "My videos",

    "auth.signin": "Sign in",
    "auth.signup": "Sign up",
    "auth.signout": "Sign out",
    "auth.title.signin": "Sign in",
    "auth.title.signup": "Create account",
    "auth.subtitle": "An account is needed to create videos.",
    "auth.email": "Email",
    "auth.password": "Password",
    "auth.needAccount": "No account? Sign up",
    "auth.haveAccount": "Already have an account? Sign in",
    "auth.google": "Continue with Google",
    "auth.or": "or",
    "auth.checkEmail": "Account created. Confirm your email before signing in.",
    "auth.wrongPassword": "Incorrect email or password.",

    "step1.badge": "1",
    "step1.title": "What's the video about?",
    "step1.subtitle": "Describe what you want to see, in your own words",
    "step1.placeholder": "e.g. a barista pouring coffee in warm morning light",
    "step1.button": "Create photos",
    "step1.hint": "A few photos are made first — you'll pick the best one",
    "step1.presets": "Ready-made ideas",

    "step2.title": "Pick one",
    "step2.subtitle": "The video will come to life from this exact photo",
    "step2.choose": "Choose this",
    "step2.chosen": "Chosen",
    "step2.empty.title": "No photos yet",
    "step2.empty.body": "Describe your video above and tap Create",

    "step3.title": "Now bring it to life",
    "step3.subtitle": "Describe the motion, if you'd like (optional)",
    "step3.placeholder": "e.g. camera slowly pushes in, steam rises",
    "step3.duration": "Length",
    "step3.duration.short": "Short",
    "step3.duration.medium": "Medium",
    "step3.duration.long": "Long",
    "step3.button": "Create video",
    "step3.gate": "Pick a photo in step 2 first",
    "step3.ready": "Photo selected. Ready to create the video.",

    "step4.title": "Your video is ready",
    "step4.subtitle": "Watch it, download it, or make it better",
    "step4.download": "Download",
    "step4.enhance": "Enhance",
    "step4.empty.title": "No video yet",
    "step4.empty.body": "Your finished video will appear here",

    "history.title": "My videos",
    "history.subtitle": "Everything you've made, newest first",
    "history.signin.title": "Sign in required",
    "history.signin.body": "Your videos are private to your account",
    "history.empty.title": "Nothing yet",
    "history.empty.body": "Photos and videos you create will show up here",
    "history.failed": "Couldn't load",
    "history.badge.video": "Video",
    "history.badge.image": "Photo",

    "confirm.title": "Confirm the price",
    "confirm.note": "This spends real money. Only charged if it succeeds.",
    "confirm.cancel": "Cancel",
    "confirm.ok": "Yes, start",

    "postprod.title": "Enhance video",
    "postprod.op": "What do you want to do?",
    "postprod.op.upscale": "Improve quality",
    "postprod.op.bgremove": "Remove background",
    "postprod.op.subtitles": "Add subtitles",
    "postprod.op.lipsync": "Match sound to mouth",
    "postprod.cancel": "Cancel",
    "postprod.getprice": "See price",
    "postprod.upscale.quality": "Quality level",
    "postprod.upscale.q1": "Good",
    "postprod.upscale.q2": "Very good",
    "postprod.upscale.q3": "Best",
    "postprod.bgremove.color": "Background color",
    "postprod.subtitles.lang": "Language (optional)",
    "postprod.subtitles.langPlaceholder": "Leave blank to auto-detect",
    "postprod.lipsync.audio": "Audio file link",
    "postprod.lipsync.audioPlaceholder": "https://... a direct link to the audio file",
    "postprod.error.audioRequired": "Paste a link to the audio file first.",

    "common.working": "Working…",
    "common.workingHint": "You can leave this tab open — progress keeps updating",
    "common.retry": "Try again",
    "common.errorTitle": "Something went wrong",
    "common.connectError": "Could not reach the server. Is it running?",

    "health.falKeyMissing": "fal.ai key not configured",
    "health.ffmpegMissing": "ffmpeg not found",

    "toast.describeFirst": "Describe the video first",
    "toast.pickFrame": "Pick a photo in step 2 first",
    "toast.signInFirst": "Sign in to continue",
    "toast.videoReady": "Video ready!",
    "toast.enhanceReady": "Ready — see it in My videos",
    "toast.enhanceFailed": "That enhancement failed",
    "toast.priceChanged": "The price changed, please confirm again",
    "toast.presetApplied": "applied",
    "toast.signedIn": "Welcome",
    "toast.starting": "Starting…",
    "toast.noImages": "No photos came back. Try describing it differently.",
    "toast.noVideo": "No video came back.",

    "preset.product_reveal.name": "Product reveal",
    "preset.product_reveal.blurb": "Slow push-in, soft light",
    "preset.talking_head.name": "Portrait",
    "preset.talking_head.blurb": "Face stays clear, natural movement",
    "preset.cinematic_reveal.name": "Cinematic reveal",
    "preset.cinematic_reveal.blurb": "Camera rises, the scene opens up",
    "preset.action_beat.name": "Action scene",
    "preset.action_beat.blurb": "Fast, continuous movement",
    "preset.ambient_loop.name": "Background style",
    "preset.ambient_loop.blurb": "Gentle, calm movement",
  },
};

const LANG_NAMES = { uz: "O'zbek", ru: "Русский", en: "English" };

function detectLang() {
  try {
    const saved = localStorage.getItem("vf_lang");
    if (saved && I18N[saved]) return saved;
  } catch { /* private mode etc. */ }
  const nav = (navigator.language || "uz").slice(0, 2).toLowerCase();
  return I18N[nav] ? nav : "uz";
}

let currentLang = detectLang();

function t(key) {
  return (I18N[currentLang] && I18N[currentLang][key]) || I18N.en[key] || key;
}

function setLang(lang) {
  if (!I18N[lang]) return;
  currentLang = lang;
  try { localStorage.setItem("vf_lang", lang); } catch { /* ignore */ }
  document.documentElement.lang = lang;
  applyI18n();
}

/* Fills every [data-i18n] element's text and every [data-i18n-placeholder]
   element's placeholder from the current dictionary. Dynamic JS-built
   content (cards, toasts) calls t() directly instead. */
function applyI18n() {
  document.querySelectorAll("[data-i18n]").forEach((el) => {
    el.textContent = t(el.getAttribute("data-i18n"));
  });
  document.querySelectorAll("[data-i18n-placeholder]").forEach((el) => {
    el.setAttribute("placeholder", t(el.getAttribute("data-i18n-placeholder")));
  });
  const picker = document.getElementById("lang-picker");
  if (picker) picker.value = currentLang;
}
