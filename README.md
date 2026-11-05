# Conspirators of Rome — Bot Logic

Oyunun (bir Secret Hitler esintili, Roma Senatosu temalı gizli-rol oyunu)
bot yapay zekâsının **kurallarının** saf Python portu. Arayüz, diyalog/
bluff metinleri yok — sadece botların neye göre karar verdiği.

## Dosyalar

- `bot_logic.py` — kuralların kendisi. Her fonksiyon kısa bir docstring
  ile neden o şekilde çalıştığını açıklar.
- `simulate.py` — `bot_logic.py`'daki kuralları kullanarak tam oyunlar
  simüle eder, zorluk seviyesine göre kazanma oranlarını basar:
  ```
  python simulate.py                     # varsayılan: hücre başına 2000 oyun, 5-8 kişi
  python simulate.py --games 500 --counts 5 6
  ```

## Temel model

Botlar gizli rolleri asla doğrudan görmez. Her botun "bilgisi" iki
katmandan oluşur:

1. **Kamuya açık kanıt** — masadaki herkesin yeniden kurabileceği bilgi:
   kim Princeps/Consul oldu, hangi kart çıktı, kim nasıl oy verdi. Her
   oyuncu, diğer herkes için 0-100 arası bir "şüphe skoru" tutar ("bu
   oyuncu ne kadar Conspiracy gibi görünüyor"). Bu skor, kart sayımına
   dayalı bir "bu sonuç ne kadar kaçınılmazdı" olasılığıyla ağırlıklanır:
   deste zaten Imperium'la doluyken çıkan kötü bir karar az şüpheli,
   deste hâlâ Res Publica doluyken çıkan aynı karar çok şüphelidir.
2. **Özel kesinlik** — bir "Investigate Loyalty" sonucu, veya elinde
   gerçek bir seçeneği (RP+IM) olup yine de Imperium'u oynayan bir
   Consul'u yakalamak. Bu kanıtlanabilir, olasılıksal değil — ilgili
   gözlemci için şüphe skorunu tamamen geçersiz kılar.

## Zorluk nasıl işliyor

`skill_prob(bot, state)` her karardan önce tek bir olasılık döndürür: bu
botun gerçek stratejik mantığı mı kullanacağı, yoksa basit/takım-tutarlı
bir varsayılana mı düşeceği. Zorluk seviyesi bu olasılığı belirler —
rastgelelik eklemez, sadece "iyi okuma" ne sıklıkla devreye giriyor onu
ayarlar. **Kart seçimi bu zardan tamamen bağımsızdır** — her bot her
zaman kendi takımına yarayan kartı tutar; zar sadece bir Conspirator'ın
kendini aklamak için bilerek yanlış kartı oynamasını (cover) yönetir.

## Sınırlamalar (bilerek dışarıda bırakılanlar)

- Diyalog/bluff katmanı (botların "no choice" gibi yalan beyanları) yok —
  sadece gerçek kararlar.
- `simulate.py`'daki Special Election ve Foresight (peek) basitleştirilmiş/
  etkisiz; asıl oyunun JS kodunda daha gelişmiş.
- Küçük masa (2-4 kişi) kuralları henüz portlanmadı — sadece standart
  5-8 kişilik oyunlar simüle ediliyor.
- `simulate.py`'daki kazanma oranları kaba bir sağlık kontrolüdür, asıl
  oyunun dengeleme raporuyla bire bir eşleşmesi beklenmez.

## Gereksinimler

Python 3.10+, ek bağımlılık yok (sadece standart kütüphane).
