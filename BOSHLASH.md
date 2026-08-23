# Boshlash — sizga faqat shu kerak

Bu faylda **faqat siz qiladigan ishlar** bor. Qolgan hamma narsani men qilaman.

---

## ✅ Bajarilgan (siz hech narsa qilmadingiz)

| Ish | Holat |
|---|---|
| Supabase loyihasini uyg'otish | ✅ Tiklandi, ishlayapti |
| 7 ta jadval yaratish | ✅ `projects`, `characters`, `locations`, `templates`, `scenes`, `assets`, `generations` |
| 2 ta hisobot | ✅ `project_economics`, `scene_economics` |
| Video montaj dasturi (ffmpeg) | ✅ O'rnatildi va sinovdan o'tdi |
| Konveyer skripti | ✅ Yozildi va sinovdan o'tdi |

---

## ✅ Tarmoq ochildi — endi to'siq bitta: balans

**2026-08-09 da tekshirildi.**

| Tekshiruv | Natija |
|---|---|
| Tarmoq: `fal.ai`, `fal.run`, `queue.fal.run`, `rest.alpha.fal.ai`, `v3.fal.media` | ✅ Ochiq (403 yo'q) |
| `FAL_KEY` muhitda bormi | ✅ Bor |
| Kalit haqiqiymi | ✅ Ha — fal uni tanidi |
| Rasm/video yasash | ❌ **403 — "User is locked. Reason: Exhausted balance."** |

Ya'ni: eski to'siq (tarmoq bloki) ketdi, kalit ham to'g'ri. Lekin fal.ai
akkauntida **pul qolmagan**, shuning uchun akkaunt qulflangan va har qanday
so'rovni rad etyapti.

> Eslatma: `fal.media` (apex domen) ulanmaydi, lekin bu muammo emas — u
> haqiqatda mavjud emas. Fayllar `v3.fal.media` dan keladi, u ishlayapti.

### Buni siz ochasiz — 3 ta bosish

1. Oching: **https://fal.ai/dashboard/billing**
2. **Add credit** tugmasini bosing
3. **$10** yozing va to'lang

Pul tushgandan keyin menga **"pul soldim"** deb yozing — men shu yerda,
shu sessiyada darhol tekshiraman. Yangi sessiya ochish **shart emas**.

> $10 taxminan 60–80 ta arzon harakat testiga yetadi. Birinchi video uchun
> bu yetarlidan ko'p. Ko'proq solmang.

---

## 🔑 fal.ai kaliti — ✅ bajarilgan

> Kalit allaqachon muhitga `FAL_KEY` sifatida qo'yilgan va ishlayapti.
> Quyidagi qadamlar faqat kelajakda kalitni almashtirish kerak bo'lsa asqotadi.

### 1-qadam. Kalit oling

1. Brauzerda oching: **https://fal.ai**
2. **Sign up** tugmasini bosing (Google akkaunt bilan kirsangiz ham bo'ladi)
3. Kirgandan keyin oching: **https://fal.ai/dashboard/keys**
4. **Add key** tugmasini bosing
5. Chiqqan uzun matnni **nusxalang** (u faqat bir marta ko'rsatiladi!)

### 2-qadam. Pul soling — $10, ko'p emas

1. Oching: **https://fal.ai/dashboard/billing**
2. **Add credit** tugmasini bosing
3. **$10** yozing va to'lang

> $10 taxminan 60–80 ta arzon harakat testiga yetadi. Birinchi video uchun
> bu yetarlidan ko'p. Ko'proq solmang.

### 3-qadam. Kalitni menga bering

Ikkita yo'l bor — **birinchisi tavsiya qilinadi**:

**A yo'l (tavsiya):** claude.ai/code sahifasida bu loyihaning
**Environment** (Muhit) sozlamalarini oching → **Environment variables** →
yangi qator qo'shing:

- Nom: `FAL_KEY`
- Qiymat: (nusxalagan kalitingiz)

Saqlang. Bu yo'l yaxshiroq — kalit doimiy saqlanadi.

**B yo'l (tez):** Kalitni shu chatga tashlang, men o'zim kerakli joyga
joylashtiraman. Faqat shuni biling: bu holda kalit suhbat tarixida qoladi,
shuning uchun keyinroq fal.ai'da uni o'chirib, yangisini yasab qo'ying.

---

## 🎬 Keyin nima bo'ladi

Kalit kelgandan keyin menga shunchaki **g'oyani ayting**. Masalan:

> "Toshkentdagi kofeshop haqida 30 soniyalik reklama, men gapiraman"

Shundan keyin men o'zim:

1. Sahnalar ro'yxatini yozaman va bazaga saqlayman
2. Har bir yuz ko'rinadigan sahna uchun **rasm** yasayman (arzon)
3. Rasmni sizga ko'rsataman — siz faqat "ha" yoki "yo'q" deysiz
4. Tasdiqlangan rasmdan **harakat testi** qilaman (~$0.15)
5. Test yaxshi chiqsa — **narxni aytaman va sizdan ruxsat so'rayman** ⬅️ *faqat shu joyda*
6. Ruxsatdan keyin yakuniy videoni olaman
7. Ovoz qo'shib, hammasini bitta videoga yig'aman
8. Ishlagan sahnalarni shablonga saqlayman — keyingi video arzonroq bo'ladi

**Pul faqat 5-qadamdan keyin sarflanadi va faqat sizning ruxsatingiz bilan.**

---

## 🖥️ Ixtiyoriy: Claude Desktop'ga fal.ai ulash

Bu **shart emas** — men fal.ai bilan to'g'ridan-to'g'ri ishlayapman.
Lekin o'z kompyuteringizdagi Claude Desktop'da ham ishlatmoqchi bo'lsangiz:

1. Claude Desktop'ni oching
2. **Settings → Developer → Edit Config** ni bosing
3. Ochilgan faylga quyidagini joylashtiring (`your-fal-api-key` o'rniga
   kalitingizni qo'ying):

```json
{
  "mcpServers": {
    "fal-ai": {
      "command": "npx",
      "args": ["-y", "fal-ai-mcp"],
      "env": {
        "FAL_KEY": "your-fal-api-key"
      }
    }
  }
}
```

4. Faylni saqlang va Claude Desktop'ni **to'liq yopib, qayta oching**

Fayl joylashuvi (qo'lda topmoqchi bo'lsangiz):
- Mac: `~/Library/Application Support/Claude/claude_desktop_config.json`
- Windows: `%APPDATA%\Claude\claude_desktop_config.json`

---

## ⚠️ Bir ogohlantirish (Supabase)

Sizning Supabase bazangizda **boshqa loyihaning 22 ta jadvali** bor
(`Institution`, `User`, `Review` va h.k. — ta'lim muassasalari haqida).
Men ularga tegmadim.

Lekin ularning **himoyasi o'chirilgan** — ya'ni bazangiz ochiq kalitini
biladigan har kim o'sha jadvallarni o'qiy va o'zgartira oladi.
Mening yangi jadvallarim himoyalangan.

Bu men hal qiladigan ish emas — o'sha eski loyihaga tegishli.
Xohlasangiz, alohida aytasiz, men o'sha jadvallarni ham himoyalab beraman.
