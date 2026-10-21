import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from vitalwatch import scrubber  # noqa: E402


class ScrubberTest(unittest.TestCase):
    def test_email_is_masked(self):
        self.assertEqual("[email] ile giris", scrubber.text("ahmet@ornek.com ile giris", 500))

    def test_email_with_turkish_letters_is_masked(self):
        # \p{L} karşılığı Python'da \w ile kuruldu; Türkçe harfler kapsanmalı.
        self.assertEqual("[email]", scrubber.text("şükrü@örnek.com.tr", 500))

    def test_email_in_path_does_not_swallow_path(self):
        # Gevşek desen ([^\s@]+) bütün yolu maskeliyordu.
        self.assertEqual("/user/[email]/profile", scrubber.path("/user/a@b.com/profile"))

    def test_query_string_atilir(self):
        self.assertEqual("/search", scrubber.path("/search?q=masked&token=abc"))

    def test_credentials_are_masked(self):
        self.assertEqual("[national_id]", scrubber.text("12345678901", 500))
        self.assertEqual("[phone]", scrubber.text("05321234567", 500))
        self.assertEqual("[iban]", scrubber.text("TR330006100519786457841326", 500))
        self.assertEqual("[card]", scrubber.text("4111 1111 1111 1111", 500))

    def test_long_token_is_masked(self):
        self.assertEqual("[token]", scrubber.text("a" * 40, 500))

    def test_short_word_not_masked(self):
        self.assertEqual("ValueError kisa mesaj", scrubber.text("ValueError kisa mesaj", 500))

    def test_sql_literalleri_normalize_edilir(self):
        self.assertEqual(
            "select * from users where email = ? and id = ?",
            scrubber.sql("select * from users where email = 'a@b.com' and id = 42"),
        )

    def test_in_list_collapses_to_single_placeholder(self):
        self.assertEqual("id IN (?)", scrubber.sql("id IN (1, 2, 3)"))

    def test_value_does_not_leak_in_sql_message(self):
        message = scrubber.message(
            "duplicate key value violates constraint: Key (email)=('a@b.com') exists",
            contains_sql=True,
        )

        self.assertNotIn("a@b.com", message)

    def test_limit_uygulanir(self):
        # Boşluklu metin: tek parça uzun dizi olsaydı önce [token] olurdu.
        self.assertEqual(10, len(scrubber.text("ab " * 100, 10)))

    def test_empty_value_returns_none(self):
        self.assertIsNone(scrubber.text("", 10))
        self.assertIsNone(scrubber.path(None))
        self.assertIsNone(scrubber.sql(""))


    # Yükleme dosya adları okunur kalmalı: zaman damgası kart, uzun ad jeton
    # sanılıyordu. Beklenenler hub Scrubber'ıyla ortak vektörlerden.
    def test_upload_filename_stays_readable(self):
        for path in (
            "/uploads/discount/kampanya-gorseli-yaz-indirimi-1726571234567-752066249.jpeg",
            "/uploads/sliders/slider-slider-1726571234567-249230604-1726571239999-875783343.png",
            "/uploads/products/urun-1795123456789-123456789.png",
        ):
            self.assertEqual(path, scrubber.path(path))

    def test_card_masked_only_if_real(self):
        self.assertEqual("kart [card] red", scrubber.text("kart 4111111111111111 red", 500))
        self.assertEqual("[card] red", scrubber.text("378282246310005 red", 500))
        self.assertEqual("saat 1726571234567 geçti", scrubber.text("saat 1726571234567 geçti", 500))
        self.assertEqual("no 4111111111111112", scrubber.text("no 4111111111111112", 500))
        # Luhn'u tutan damga: yalnızca ilk hane kuralı ayırıyor.
        self.assertEqual("saat 1726571234573 geçti", scrubber.text("saat 1726571234573 geçti", 500))

    def test_phone_with_90_prefix_is_masked(self):
        # Sol sınır eklenince + olmadan 90 ön eki sızıyordu (önceden "9[phone]").
        self.assertEqual("tel [phone]", scrubber.text("tel 905321234567", 500))
        self.assertEqual("tel [phone]", scrubber.text("tel 90 532 123 45 67", 500))

    def test_random_strings_masked_by_new_rule(self):
        for input in (
            "x 3f2a1b4c-5d6e-4f70-8a9b-0c1d2e3f4a5b y",
            "x ghp_16C7e42F292c6912E7710c838347Ae178B4a y",
            "x a1b2c3d4-e5f6a7b8-c9d0e1f2-a3b4c5d6 y",
        ):
            self.assertEqual("x [token] y", scrubber.text(input, 500))

if __name__ == "__main__":
    unittest.main()
