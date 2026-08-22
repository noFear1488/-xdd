import tempfile
import unittest
from pathlib import Path

from yandex_nosite.models import Business
from yandex_nosite.outreach import (
    DEFAULT_BENEFIT,
    PhoneError,
    benefit_for,
    is_mobile,
    normalize_phone,
    prepare,
    render,
    subject_for,
    telegram_link,
    telegram_uri,
    to_markdown,
)
from yandex_nosite.storage import Storage


def biz(name="Салон", category="салон красоты", phone="+7 925 516-27-15", **kwargs):
    return Business(
        company_id=kwargs.pop("cid", "osm:node/1"),
        name=name,
        categories=[category] if category else [],
        phones=[phone] if phone else [],
        **kwargs,
    )


class PhoneTest(unittest.TestCase):
    def test_normalizes_russian_formats(self):
        for raw in ["+7 925 5162715", "8 925 516 27 15", "89255162715", "9255162715"]:
            self.assertEqual(normalize_phone(raw), "79255162715", raw)

    def test_rejects_garbage(self):
        with self.assertRaises(PhoneError):
            normalize_phone("не телефон")

    def test_mobile_vs_landline(self):
        self.assertTrue(is_mobile("+7 925 5162715"))
        self.assertTrue(is_mobile("89160220634"))
        # Городские и сервисные номера к Telegram не привязывают.
        self.assertFalse(is_mobile("+7 495 7419765"))
        self.assertFalse(is_mobile("+7 499 8775686"))
        self.assertFalse(is_mobile("8 800 3014767"))
        self.assertFalse(is_mobile("+7 4967 646250"))

    def test_links(self):
        self.assertEqual(telegram_link("8 925 516-27-15"), "https://t.me/+79255162715")
        self.assertEqual(
            telegram_uri("8 925 516-27-15"), "tg://resolve?phone=79255162715"
        )


class PersonalizationTest(unittest.TestCase):
    def test_benefit_matches_category(self):
        self.assertIn("запись", benefit_for(biz(category="стоматология")))
        self.assertIn("меню", benefit_for(biz(category="кафе")))

    def test_unknown_category_gets_default_benefit(self):
        self.assertEqual(benefit_for(biz(category="рыболовный")), DEFAULT_BENEFIT)

    def test_longest_category_match_wins(self):
        """«ветклиника» содержит «клиника» — выбирать нужно точное совпадение."""
        self.assertIn("ветклинику", subject_for(biz(category="ветклиника")))
        self.assertIn("дежурного врача", benefit_for(biz(category="ветклиника")))

    def test_message_mentions_name_and_benefit(self):
        message = render(biz(name="ИРИС", category="салон красоты"))
        self.assertIn("ИРИС", message.text)
        self.assertIn("портфолио работ", message.text)
        self.assertEqual(message.link, "https://t.me/+79255162715")

    def test_message_offers_opt_out(self):
        """Возможность отказаться — обязательная часть холодного обращения."""
        self.assertIn("не надо", render(biz()).text)

    def test_custom_template(self):
        message = render(biz(name="Кафе Уют", category="кафе"), "Привет, {title}! {benefit}")
        self.assertTrue(message.text.startswith("Привет, Кафе Уют!"))


class PrepareTest(unittest.TestCase):
    def test_landlines_skipped_with_reason(self):
        messages, skipped = prepare([biz(phone="+7 495 7419765")])
        self.assertEqual(messages, [])
        self.assertIn("не мобильный", skipped[0][1])

    def test_any_phone_mode_includes_landlines(self):
        messages, _ = prepare([biz(phone="+7 495 7419765")], mobile_only=False)
        self.assertEqual(len(messages), 1)

    def test_missing_phone_skipped(self):
        _, skipped = prepare([biz(phone=None)])
        self.assertEqual(skipped[0][1], "нет телефона")

    def test_batch_limit_respected(self):
        items = [biz(cid=f"osm:node/{i}") for i in range(30)]
        messages, _ = prepare(items, limit=15)
        self.assertEqual(len(messages), 15)

    def test_markdown_has_checkboxes_and_links(self):
        messages, _ = prepare([biz()])
        text = to_markdown(messages)
        self.assertIn("- [ ] отправлено", text)
        self.assertIn("https://t.me/+79255162715", text)
        self.assertIn("вручную", text)


class OutreachTrackingTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Storage(Path(self.dir.name) / "t.db")

    def tearDown(self):
        self.db.close()
        self.dir.cleanup()

    def test_marks_and_lists_contacted(self):
        self.db.mark_outreach("osm:node/1", "sent")
        self.assertEqual(self.db.contacted_ids(), {"osm:node/1"})

    def test_status_updated_not_duplicated(self):
        self.db.mark_outreach("osm:node/1", "sent")
        self.db.mark_outreach("osm:node/1", "replied", "просят примеры")
        self.assertEqual(self.db.outreach_counts(), {"replied": 1})

    def test_counts_by_status(self):
        self.db.mark_outreach("a", "sent")
        self.db.mark_outreach("b", "refused")
        self.assertEqual(self.db.outreach_counts(), {"sent": 1, "refused": 1})

    def test_channels_are_separate(self):
        self.db.mark_outreach("a", "sent", channel="telegram")
        self.db.mark_outreach("a", "called", channel="phone")
        self.assertEqual(self.db.contacted_ids("telegram"), {"a"})
        self.assertEqual(self.db.outreach_counts("phone"), {"called": 1})


if __name__ == "__main__":
    unittest.main()
