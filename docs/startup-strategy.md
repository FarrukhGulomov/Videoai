# Strategiya — "Higgsfield/fal.ai'dan arzon, o'zbek segmentiga moslashgan" video-startup

> Tayyorlangan sana: 2026-08-09. Narxlar internetdan olingan, tez o'zgaradi —
> ishga tushirishdan oldin har doim rasmiy narxlarni qayta tekshiring.

---

## 1. Bitta jumlada xulosa

**fal.ai'ni "aylanib o'tish" arzonlashtirmaydi — fal allaqachon Kling/Veo/Runway'ga
eng arzon yo'llardan biri bo'lib kirish beradi.** Haqiqiy tejov ikki joydan keladi:
(1) **har bir kadr uchun to'g'ri modelni tanlash** (qimmat model faqat yuz
muhim bo'lgan kadrlarga, arzon model harakat/fon kadrlariga), va
(2) **Uzbek ovoz uchun ElevenLabs emas, mahalliy TTS** ishlatish — chunki
ElevenLabs Uzbek tilini umuman qo'llab-quvvatlamaydi.

---

## 2. Video modellar — narxlarni solishtirish

| Model (fal.ai orqali) | Narx / soniya | Eng yaxshi ishlatilishi |
|---|---|---|
| Veo 3.1 Lite (ovozsiz) | ~$0.03/s | Arzon test (rung 2) — bizda allaqachon shu |
| Kling 2.6 Pro (ovozsiz) | ~$0.07/s | O'rta daraja: fon/harakat kadrlari, yuz muhim bo'lmagan planlar |
| Kling 2.6 Pro (ovozli) | ~$0.14/s | O'rta-yakuniy, ovoz kerak bo'lganda |
| Veo 3.1 (to'liq, ovozli) | ~$0.15/s | Yuz muhim, "hero" kadrlar — biz aynan shuni ishlatdik |
| Kling 3.0 (4K) | ~$0.42/s | Faqat alohida buyurtma bo'lsa (juda qimmat) |

**Manba:** [Kling API narxlari — costbench.com](https://costbench.com/software/ai-media-apis/kling-api/),
[fal.ai Kling 2.6 Pro](https://fal.ai/models/fal-ai/kling-video/v2.6/pro/image-to-video),
[Veo 3.1 narxlari — veo3gen.app](https://www.veo3gen.app/blog/veo-3-1-pricing-plans),
[fal vs Replicate — teamday.ai](https://www.teamday.ai/blog/fal-ai-vs-replicate-comparison).

**Xulosa:** fal.ai'dan chiqib Google'ning o'z Vertex AI'siga to'g'ridan-to'g'ri
ulanish narxni **pasaytirmaydi** — ba'zi manbalarga ko'ra Vertex to'g'ridan-to'g'ri
hatto qimmatroq ($0.40–0.75/s) chiqishi mumkin, ustiga enterprise hisob-kitob,
kvota va billing murakkabligi qo'shiladi. **fal.ai'da qolib, faqat model
tanlovini kengaytirish (Kling qo'shish) — eng arzon va eng tez yo'l.**

---

## 3. Ovoz — nega ElevenLabs bu yerda ishlamaydi

ElevenLabs Text-to-Speech ro'yxatida **o'zbek tili yo'q** (faqat Speech-to-Text
— ya'ni ovozdan matnga — o'zbekchani tushunadi, lekin matndan ovoz **yasay
olmaydi**). Demak, o'zbek foydalanuvchilar uchun ElevenLabs bilan ovoz qo'shish
= yo umuman ishlamaydi, yoki talaffuz noto'g'ri chiqadi.

| Xizmat | Narx | Izoh |
|---|---|---|
| **Aisha AI** (aisha.group) | ~1 so'm/belgi (~$0.00008/belgi) | O'zbekiston'ning o'z platformasi, tabiiy talaffuz, REST API bor |
| Muxlisa AI (muxlisa.uz) | E'lon qilinmagan | O'zbekiston'ning birinchi STT/TTS platformasi, biznes uchun |
| Sayro-TTS (Hugging Face, ochiq kodli) | Bepul (o'zingiz joylashtirasiz) | Qwen3-TTS asosida, maxsus o'zbekcha uchun fine-tune qilingan |
| Facebook MMS-TTS-uzb (ochiq kodli) | Bepul | Kirill yozuvi, sifat past-o'rta |
| ElevenLabs | $0.05–0.10/1000 belgi | **O'zbekcha yo'q** — faqat rus/ingliz/turk kabi boshqa tillar uchun ishlatiladi |

**Tavsiya:** Ikki-provayderli tizim — **o'zbekcha matn → Aisha AI (yoki
Muxlisa AI)**, **boshqa tillar (ingliz, rus va h.k.) → ElevenLabs**. Bitta
"ovoz" modul ichida tilni aniqlab, to'g'ri providerga yo'naltirish kerak.

**Manba:** [ElevenLabs qo'llab-quvvatlaydigan tillar](https://help.elevenlabs.io/hc/en-us/articles/13313366263441-What-languages-do-you-support),
[Aisha AI narxlari](https://aisha.group/en/pricing), [Muxlisa AI](https://muxlisa.uz/en).

---

## 4. To'lov — Stripe emas, Payme/Click

O'zbekiston foydalanuvchilarining aksariyati mahalliy kartalar (**UzCard,
Humo**) bilan to'laydi — Stripe/PayPal bu yerda deyarli ishlamaydi.
Ishga tushirishda ikkita mahalliy shlyuzni ulash shart:

- **Payme** — eng keng tarqalgan, komissiya ~1–1.5%
- **Click** — ikkinchi eng katta, REST API, komissiya ~1–2%, ariza ko'rib
  chiqish 3–5 ish kuni

Xalqaro foydalanuvchilar uchun keyinroq Stripe qo'shiladi, lekin **MVP uchun
Payme yetarli** (Click'ni parallel ariza berib, kutish mumkin).

**Manba:** [O'zbekistondagi to'lovlar — Finextra](https://www.finextra.com/blogposting/25070/deep-dive-payments-in-uzbekistan),
[Payme/Click ulash — UZNEO](https://uzneo.uz/en/blog/payme-click-uzum-podklyuchit-oplatu).

---

## 5. Arxitektura — mavjud kod ustiga qurish

Yaxshi xabar: **bugungi `scripts/factory.py` allaqachon to'g'ri asos.**
Uning rung-1/2/3 tizimi (arzon test → tasdiq → qimmat yakuniy) — bu aynan
har qanday jiddiy video-platformaning yuragi. Startup uchun kerak bo'lgan
qo'shimchalar:

```
                     ┌─────────────────────┐
                     │   Veb-ilova (UI)     │  ← hozir yo'q, kerak
                     └──────────┬───────────┘
                                │
                     ┌──────────▼───────────┐
                     │  Foydalanuvchi/hisob  │  ← hozir yo'q (auth, wallet)
                     │  + kredit balansi     │
                     └──────────┬───────────┘
                                │
                     ┌──────────▼───────────┐
                     │   Model-router        │  ← YANGI qatlam
                     │  (qaysi kadr → qaysi  │
                     │   model: Veo/Kling)   │
                     └──────────┬───────────┘
                                │
              ┌─────────────────┼─────────────────┐
              ▼                 ▼                 ▼
      scripts/factory.py   Ovoz moduli        Supabase
      (fal.ai — mavjud)   (Aisha/ElevenLabs)  (mavjud, kengaytiriladi)
```

### Bosqichlar

| Bosqich | Nima qilinadi | Taxminiy vaqt |
|---|---|---|
| **0 (bugun)** | `factory.py`'ga Kling modelini qo'shish (`config.json`'da yangi model yozuvi), ovoz modulini ajratib olish | 1 kun |
| **1 — MVP** | Oddiy veb-sahifa: g'oya kiritish → sahna ro'yxati → tasdiqlash → video. Hali bitta foydalanuvchi (siz) uchun | 1–2 hafta |
| **2 — Ko'p foydalanuvchi** | Ro'yxatdan o'tish, kredit tizimi, Payme integratsiyasi | 2–3 hafta |
| **3 — O'zbek ovoz** | Aisha AI/Muxlisa AI ulash, til aniqlash, dublyaj | 1 hafta |
| **4 — Kengaytirish** | Kling qo'shish, model-router avtomatik tanlov (narx/sifat balansiga qarab) | 1–2 hafta |

---

## 6. Alohida startap sifatida — ijobiy va salbiy taraflar

### 6.1 Ijobiy taraflar

| # | Nima | Nega muhim |
|---|---|---|
| 1 | **Aniq, tor bozor bo'shlig'i** | O'zbek/Markaziy Osiyo tilida ovoz + mahalliy to'lov (Payme/Click) bilan ishlaydigan AI-video mahsuloti amalda yo'q. Higgsfield/Runway/Pika bu segmentga maxsus mo'ljallanmagan. |
| 2 | **Texnik asos allaqachon isbotlangan** | Bugungi sessiyada `factory.py`'ning rung-1/2/3 tizimi, model-router (Kling/Veo/Seedance), narx-nazorat mexanizmi haqiqiy pul bilan sinovdan o'tdi — noldan boshlash emas. |
| 3 | **Past boshlang'ich kapital** | O'z video-modelingizni o'qitish shart emas — fal.ai, Aisha AI kabi tayyor API'lar ustiga qurilyapti. Infratuzilma xarajati deyarli yo'q. |
| 4 | **Kengayish yo'nalishlari ko'p** | Bitta "video generator"dan boshlab — reklama studiyasi, ijtimoiy tarmoq kontent-fabrikasi, ta'lim videolari, korporativ taqdimotlarga kengaytirish mumkin. |

### 6.2 Salbiy taraflar / xavflar

| # | Xavf | Ta'siri | Yumshatish yo'li |
|---|---|---|---|
| 1 | **Marja provayderga bog'liq** | fal.ai/Kling/Veo narxni istalgan payt oshirishi mumkin — sizning marjangiz ularning qo'lida. Bugun aynan shuni ko'rdik: $10 taxminan bitta 20 soniyalik videoga yetdi. | Narxni kredit sifatida oldindan qulflab sotmang — "taxminiy narx" ko'rsating, real vaqtda hisoblang, yoki marjani keng qoldiring (2-3x). |
| 2 | **Vendor lock-in** | Butun biznes fal.ai/Higgsfield kabi uchinchi tomon API'siga tayanadi. Ular narx siyosatini, kirish shartlarini, hatto API'ni butunlay yopishi mumkin. | Model-router allaqachon ko'p-provayderli (Kling/Veo/Seedance) — shu tamoyilni davom ettirib, bitta provayderga qattiq bog'lanib qolmaslik. |
| 3 | **Sifat barqarorligi — hal qilinmagan muammo** | Bugun ko'p marta ko'rdik: yuz o'xshamay qoladi, harakat sun'iy chiqadi. Bu — butun AI-video sohasining hozirgi chegarasi, faqat sizning kodingiz emas. Ko'p foydalanuvchili mahsulotda bu **shikoyat va pul qaytarish** oqimiga aylanadi. | Har doim 3+ variant + avtomatik yuz-solishtirish, "sizga yoqmasa qayta ishlaymiz" siyosati, foydalanuvchini oldindan ogohlantirish. |
| 4 | **Huquqiy va obro' xavfi (deepfake)** | Foydalanuvchi boshqa birovning yuzini yuklab video yasashi mumkinmi? O'zbekistonda bu bo'yicha aniq qonunchilik hali yo'q — noaniqlik o'zi xavf. | MVP uchun "faqat o'z rasmingiz" siyosati + yuklashda soddaroq tasdiqlash (masalan, selfie bilan tekshirish). |
| 5 | **Kuchli raqobat** | Higgsfield, Runway, Pika, Kling'ning o'z ilovasi, HeyGen — bularning barchasida katta jamoa va investitsiya bor. O'zbek tili ustunligi vaqtinchalik bo'lishi mumkin (ular ham qo'shishi mumkin). | Faqat tilda emas, **tezlik va narxda** ustunlik qilish — mahalliy to'lov, mahalliy qo'llab-quvvatlash, past narx. |
| 6 | **Pul oqimi xavfi (kredit oldindan sotish)** | Foydalanuvchi kredit sotib olgandan keyin fal/Kling narxi oshib ketsa — oldin sotilgan kreditlar zarar keltiradi (narx-arbitraj xavfi). | Kreditni "taxminiy" deb belgilang, katta narx o'zgarishida avtomatik qayta hisoblash mexanizmi kiriting. |
| 7 | **Operatsion yuk ortadi** | Bitta-foydalanuvchi skriptdan farqli — ko'p foydalanuvchili tizim endi: content-moderatsiya, xato holatlari, navbat boshqaruvi, mijozlarga yordam talab qiladi. | Bosqichma-bosqich o'sish (roadmap'dagi kabi) — avval o'zingiz sinab, keyin oz sonli "yopiq beta" foydalanuvchilar bilan. |
| 8 | **Vaqt va e'tibor narxi** | MVP ham bir necha hafta talab qiladi, useful foydalanuvchi bazasi yig'ish esa oylar. Bu joriy loyihalaringizdan vaqt/e'tibor olib qo'yadi. | Kichik, aniq maqsad bilan boshlash (1-bosqich, 1 foydalanuvchi — siz), katta sarmoya kiritmasdan sinash. |

### 6.3 Qisqa xulosa

Eng katta ikkita xavf — **(1) marja/narx nazorati sizning qo'lingizda emas** va
**(2) sifat barqarorligi hali AI-video sohasining o'zida hal qilinmagan
muammo**. Ikkalasi ham "qurilish" bilan emas, balki **biznes-model va
kutish boshqaruvi** bilan yumshatiladi — foydalanuvchidan haqiqiy narxni
yashirmaslik, va "kafolatlangan mukammal video" emas, "arzon va tez sinov"
sifatida taqdim etish.

---

## 7. Keyingi qadam

Ushbu hujjat — reja, kod emas. Qaysi bosqichdan boshlashni xohlaysiz:

1. `factory.py`'ga Kling modelini qo'shish va narx-sifatni solishtirib ko'rish (bugun, arzon)
2. Oddiy veb-UI (MVP, 1-bosqich) qurishni boshlash
3. Ovoz modulini (Aisha AI) ulashni sinab ko'rish
