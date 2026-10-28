# גיבוי חיצוני: cron-job.org

> מסמך תפעול. אושר על ידי הבעלים ב-28.10.2026. אין בו ואסור שיהיו בו פרטי גישה. הטוקן נשמר רק אצל cron-job.org.

## למה
המתזמן של GitHub עיכב או דילג על ריצות (סעיף 7.3 ב-SPEC), וגם הגיבוי הפנימי (`scan-watchdog.yml`) תלוי באותו מתזמן. cron-job.org שולח בקשת הפעלה ישירה ל-GitHub, מחוץ למתזמן של GitHub. ניסיון קודם עם Claude Routine נכשל, כי לסשן שה-Routine פתח לא היה כלי GitHub.

## מה הבקשה עושה
| | |
|---|---|
| כתובת | `https://api.github.com/repos/rotmansh/crypto-radar/actions/workflows/data-sync.yml/dispatches` |
| שיטה | `POST` |
| כותרות | `Authorization: Bearer <הטוקן>` · `Accept: application/vnd.github+json` · `X-GitHub-Api-Version: 2022-11-28` · `Content-Type: application/json` |
| גוף | `{"ref":"main","inputs":{"trigger":"external_cron","reason":"cron-job.org"}}` |
| תשובה תקינה | `204 No Content` (גוף ריק). כל תשובה 2xx היא הצלחה |
| תזמון | 25 דקות אחרי כל סגירת נר 4H, לפי UTC: 00:25, 04:25, 08:25, 12:25, 16:25, 20:25 (בשעון חורף בישראל: 02:25, 06:25 … 22:25) |

הבקשה לא סורקת ולא כותבת כלום בעצמה. היא רק מפעילה את ה-workflow הרגיל, ושם רצים כל הבדיקות, השער ומנגנוני הבטיחות.

## הרשאות הטוקן (Fine-grained PAT)
- **Repository access:** Only select repositories → `rotmansh/crypto-radar` בלבד.
- **Repository permissions:** `Actions: Read and write` בלבד. GitHub מוסיף `Metadata: Read-only` אוטומטית, וזה חובה.
- **בלי שום הרשאה אחרת**, ובמיוחד בלי `Contents`. עם הטוקן הזה אי אפשר לשנות קוד או נתונים.
- **אם הטוקן ידלוף:** אפשר רק להפעיל, לבטל או למחוק ריצות של ה-workflows בריפו הזה, ולקרוא את הלוגים שלהם (הריפו ציבורי בכל מקרה).

## רישום ומניעת כפילויות
- **רישום המקור:** ה-workflow מקבל `trigger=external_cron`. הערך נרשם ב-`data/health/runs.csv`, ב-`data/health/site-checks.csv` וב-`data/health/gate-decisions.csv`, ומוצג בדשבורד כ"גיבוי חיצוני (cron-job.org)".
- **השער:** ה-job הראשון בכל ריצה (`gate`) מדלג על הפעלה שאינה ידנית אם לחלון הנוכחי כבר יש ריצה מוצלחת. הדילוג נרשם עם המקור והסיבה.
- **הפעלות חופפות:** כל ריצות ה-workflow נמצאות באותה קבוצת `concurrency` (`radar-writer`, בלי ביטול ריצה פעילה). ריצה שמגיעה בזמן שריצה אחרת פעילה ממתינה עד שהקודמת מסתיימת, ואז השער שלה רואה את התוצאה ומדלג.
- **מגבלה ידועה:** GitHub שומר רק ריצה ממתינה אחת לכל קבוצה. אם מגיעה שלישית, הממתינה הקודמת מבוטלת ומופיעה ב-Actions כ-`cancelled`. זה לא יוצר כפילות ולא משאיר חלון לא מכוסה, כי הריצה שנשארה בתור מכסה את החלון.

## ביטול
| מה | איך | השפעה |
|---|---|---|
| טוקן | GitHub → Settings → Developer settings → Personal access tokens → Fine-grained tokens → הטוקן → Delete | מיידית. כל בקשה מקבלת 401 |
| תזמון | cron-job.org → Cronjobs → הכפתור Enabled של המשימה (השבתה), או Delete | מיידית |
| חסימה בצד הריפו | הסרת `external_cron` מרשימת `options` ב-`data-sync.yml` | בקשה עם ערך לא מוכר צפויה להידחות על ידי GitHub (422) |

אם הטוקן נחשף, למשל בצילום מסך: מוחקים אותו ב-GitHub, יוצרים טוקן חדש ומעדכנים את הכותרת ב-cron-job.org.
