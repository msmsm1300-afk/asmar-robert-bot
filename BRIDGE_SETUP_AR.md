# تشغيل Bridge Agent على الجهاز الوسيط

هذا المكوّن يعمل من الجهاز الوسيط باتصال **Outbound فقط** إلى Render. لا يحتاج إلى فتح Port أو Port Forwarding، ولا ينسخ Cookies إلى Render، ولا يحاول تجاوز Cloudflare.

## المتطلبات

يجب أن يكون Python 3.11 أو أحدث وChrome/Chromium مثبتًا على الجهاز الوسيط.

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r bridge_requirements.txt
playwright install chromium
```

## المتغيرات السرية

تُضبط محليًا على الجهاز الوسيط أو عبر Secret Manager، ولا تُرفع إلى GitHub ولا تُرسل داخل Telegram:

```text
RENDER_BRIDGE_URL=https://asmar-robert-bot.onrender.com
BRIDGE_SHARED_SECRET=غيّر_هذه_القيمة
BRIDGE_TOKEN_KEY=مفتاح_Fernet_بطول_صحيح
ICHANCY_USERNAME=حساب_الـAgent
ICHANCY_PASSWORD=كلمة_مرور_الـAgent
BRIDGE_DEVICE_ID=معرّف_ثابت_للجهاز
BRIDGE_DEVICE_NAME=اسم_الجهاز
ICHANCY_CHROME_PROFILE=/مسار/ملف_Chrome_الدائم
BRIDGE_TOKEN_FILE=/مسار_آمن/bridge-tokens.enc
```

لإنشاء مفتاح Fernet مرة واحدة:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

يُخزّن زوج iChancy Token في ملف مشفّر بصلاحيات `0600` على الجهاز الوسيط. لا يطبع Bridge كلمات المرور أو Tokens في السجلات.

## تشغيل التطبيق

انسخ `.env.bridge.example` إلى `.env` على الجهاز الوسيط، واملأ القيم محليًا. بعد ذلك شغّل التطبيق:

```bash
python bridge_desktop_app.py
```

ستظهر نافذة صغيرة فيها حالة Bridge وiChancy وآخر Heartbeat وعدد Jobs، مع زري تشغيل وإيقاف وزر فتح iChancy. التطبيق يشغّل `bridge_agent.py` داخليًا ولا تحتاج إلى إبقاء نافذة Terminal مفتوحة.

## التشغيل اليدوي (للتشخيص فقط)

```bash
python bridge_agent.py
```

يعمل Bridge بهذه الدورة:

1. يرسل Heartbeat إلى Render.
2. يستعلم عن Job واحد عبر اتصال HTTPS outbound.
3. يفتح/يستخدم Chrome بملف Profile دائم على `agents.ichancy.com`.
4. ينفّذ فقط المسار الرسمي المطلوب من Agent API.
5. يعيد النتيجة إلى Render.
6. إذا انقطع الجهاز، تتحول حالته إلى `offline` بعد 90 ثانية تقريبًا.

## مسارات Render الجديدة

- `POST /bridge/v1/heartbeat`
- `POST /bridge/v1/jobs/next`
- `POST /bridge/v1/jobs/<job_id>/complete`
- `GET /bridge/status`

كلها محمية بـ `X-Bridge-Key`، والـJobs تستخدم `request_id` فريدًا لمنع إدراج العملية نفسها مرتين.

العمليات المالية لها حماية إضافية محلية: بعد نجاح `deposit` أو `withdraw` يحفظ Bridge نتيجة `request_id` في سجل مشفّر، فلا يعيد العملية إذا نجح iChancy ثم انقطع الاتصال قبل تأكيد Render.

## ترتيب الاختبار

يبدأ الاختبار بـ:

1. Heartbeat فقط.
2. اتصال Chrome وAgent API.
3. `getPlayersForCurrentAgent`.
4. `getPlayerBalanceById`.
5. `registerPlayer` على حساب اختبار.
6. بعد مراجعة النتائج فقط، اختبار إيداع وسحب بمبالغ صغيرة ومحددة.

لا تُعتبر العملية المالية ناجحة إلا عندما يعيد iChancy استجابة نجاح واضحة، وتُحفظ نتيجة العملية في Render.
