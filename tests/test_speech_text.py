import unittest

from speech_text import say_year, speakable


class SpeakableTests(unittest.TestCase):
    """Kokoro reads many number formats literally ("5.5" -> "five five"). Each expected
    string here was checked against Kokoro's phonemes."""

    def check(self, cases):
        for text, spoken in cases:
            with self.subTest(text=text):
                self.assertEqual(speakable(text), spoken)

    def test_decimals(self):
        self.check([
            ("It's 5.5 degrees.", "It's 5 point 5 degrees."),
            ("Pi is 3.14.", "Pi is 3 point 1 4."),
            ("It's 10.05 out", "It's 10 point 0 5 out"),
            ("Humidity is 62.5%.", "Humidity is 62 point 5%."),
            ("It's -2.5 tonight", "It's -2 point 5 tonight"),
        ])

    def test_thousands(self):
        self.check([
            ("That's 1,500 people.", "That's 1500 people."),
            ("1,234,567.89", "1234567 point 8 9"),
        ])

    def test_money(self):
        self.check([
            ("It's $5.", "It's 5 dollars."),
            ("Only $1", "Only 1 dollar"),
            ("It costs $5.50.", "It costs 5 dollars and 50 cents."),
            ("$1.01", "1 dollar and 1 cent"),
            ("$0.50", "50 cents"),
            ("$5.00", "5 dollars"),
            ("€12.99", "12 euros and 99 cents"),
            ("12.99€", "12 euros and 99 cents"),
            ("£3 or 50p", "3 pounds or 50p"),
            ("£0.01", "1 penny"),
            ("£2.50", "2 pounds and 50 pence"),
            ("$1,500", "1500 dollars"),
            ("$5.5 million", "5 point 5 million dollars"),
            ("$5-10", "5 to 10 dollars"),
        ])

    def test_temperatures(self):
        self.check([
            ("It's 20°C", "It's 20 degrees Celsius"),
            ("It's -3°C.", "It's -3 degrees Celsius."),
            ("Exactly 1°C", "Exactly 1 degree Celsius"),
            ("70 °F", "70 degrees Fahrenheit"),
            ("It's 14°", "It's 14°"),  # Kokoro already says "fourteen degrees"
            ("−4°C", "-4 degrees Celsius"),  # Unicode minus
        ])

    def test_units(self):
        self.check([
            ("Wind at 9 km/h", "Wind at 9 kilometers per hour"),
            ("15 mph", "15 miles per hour"),
            ("2 m/s", "2 meters per second"),
            ("5km away", "5 kilometers away"),
            ("1 km", "1 kilometer"),
            ("2.5 mm of rain", "2 point 5 millimeters of rain"),
            ("3 kg", "3 kilograms"),
        ])

    def test_times(self):
        self.check([
            ("at 3:30", "at 3 30"),
            ("at 10:05", "at 10 oh 5"),
            ("at 7:00", "at 7 o'clock"),
            ("at 7:00 pm", "at 7 pm"),
            ("at 3:30pm", "at 3 30 pm"),
            ("at 3:30 a.m.", "at 3 30 am."),
            ("at 14:45", "at 14 45"),
            ("at 7pm", "at 7 pm"),
        ])

    def test_years(self):
        self.check([
            ("in 2026", "in 20 26"),
            ("In 1999, we", "In 19 99, we"),
            ("since 1905", "since 19 oh 5"),
            ("by 1900", "by 19 hundred"),
            ("in 2005", "in 2005"),  # Kokoro already says "two thousand five"
            ("from 2020 to 2026", "from 20 20 to 20 26"),
            ("2010-2015", "20 10 to 20 15"),
            ("in March 2026", "in March 20 26"),
            ("It's 2026.", "It's 20 26."),
        ])

    def test_decades(self):
        self.check([
            ("the 1990s", "the 19 nineties"),
            ("the 2020s", "the 20 twenties"),
            ("the 2000s", "the two thousands"),
            ("the 1900s", "the 19 hundreds"),
            ("the '80s", "the eighties"),
            ("in her 50s", "in her fifties"),
        ])

    def test_ranges_fractions_and_symbols(self):
        self.check([
            ("3-4 days", "3 to 4 days"),
            ("5–10 minutes", "5 to 10 minutes"),
            ("20-25°C", "20 to 25 degrees Celsius"),
            ("1/2 cup", "one half cup"),
            ("3/4 of it", "three quarters of it"),
            ("2/3", "two thirds"),
            ("#1 fan", "number 1 fan"),
            ("2x faster", "2 times faster"),
        ])

    def test_leaves_everything_else_alone(self):
        for text in ["Version 2.0.1 is out", "Ping 192.168.1.1", "It's 5. Then 6.",
                     "Days 1, 2, and 3", "Between 1,2 and 3", "No numbers here.",
                     "on 2026-11-27", "1500 people came", "about 1850 steps", "COVID-19",
                     "4x4", "the 1st and 22nd", "50%", "a 3/8 bolt", "on 27/11/2026",
                     "10:30:15", "It's 5 to 7."]:
            with self.subTest(text=text):
                self.assertEqual(speakable(text), text)


class SayYearTests(unittest.TestCase):
    def test_say_year(self):
        self.assertEqual(say_year(2026), "20 26")
        self.assertEqual(say_year(1905), "19 oh 5")
        self.assertEqual(say_year(1900), "19 hundred")
        self.assertEqual(say_year(2000), "2000")
        self.assertEqual(say_year(2009), "2009")
        self.assertEqual(say_year(2010), "20 10")


if __name__ == "__main__":
    unittest.main()
