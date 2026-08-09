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

## 🚧 To'siq: fal.ai bloklangan

Bu bulutli muhitdan fal.ai domenlariga chiqib bo'lmaydi — tarmoq siyosati
ularni bloklagan (403). Kalit aybdor emas; so'rov domengacha yetib bormaydi.

Bloklangan domenlar:
`fal.ai`, `fal.run`, `queue.fal.run`, `rest.alpha.fal.ai`, `fal.media`, `v3.fal.media`

### Buni siz ochasiz — 8 ta bosish

1. **https://claude.ai/code** ni oching
2. Xabar yozadigan katakning **ustidagi qatorda** bulut belgisini toping —
   unda muhit nomi yozilgan (masalan `Default`). **Bosing.**
3. Ro'yxatda o'sha muhit ustiga sichqonchani olib boring → o'ng tomonda
   **tishli g'ildirak** (sozlama) belgisi chiqadi. **Bosing.**
4. Ochilgan oynada **Network access** degan joyni toping →
   **Custom** ni tanlang.
5. **Allowed domains** katagiga quyidagini **to'liq** joylashtiring
   (har biri alohida qatorda):

```
fal.ai
*.fal.ai
fal.run
*.fal.run
fal.media
*.fal.media
```

6. Pastdagi **"Also include default list of common package managers"**
   katagiga **belgi qo'ying** ✅ (bu muhim — aks holda GitHub va boshqa
   kerakli narsalar ishlamay qoladi)
7. Xuddi shu oynada **Environment variables** katagini toping va
   quyidagi qatorni qo'shing:

```
FAL_KEY=kalitingiz-shu-yerga
```

8. **Save** / **Update environment** tugmasini bosing

### ⚠️ Keyin nima qilish kerak

O'zgarish **faqat yangi sessiyalarga** ta'sir qiladi. Hozirgi suhbat eski
qoida bilan ishlayapti — shuning uchun **yangi sessiya oching** (shu
repozitoriya bilan). Men qilgan ishlarning hammasi git'da saqlangan,
yangi sessiyada davom ettiramiz.

---

## 🔑 fal.ai kaliti

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
