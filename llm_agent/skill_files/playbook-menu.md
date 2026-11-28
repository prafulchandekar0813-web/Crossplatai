---
name: playbook-menu
description: Playbook for restaurant, cafe and bakery menu apps with ordering or reservations: screens, navigation, palette options, signature features, manage.json sections, sources.
---
Use this together with app-design. Study and adapt it.

WHO IT IS FOR: hungry people on a phone who want to see the food, pick quickly and order or reserve; owners who change the menu daily.

SCREENS (Android, bottom tabs): Menu, Offers, Reserve, Orders, Contact.
- Menu: a sticky category bar (chips) that follows the scroll, dish cards with a big appetising picture, name, short description, price, veg or non-veg dot, a spicy level and an Add button; a "Chef's pick" row at the top.
- Dish sheet: picture, ingredients, add-ons and size options, special request note.
- Cart: items, notes, order type (dine-in, pickup, delivery), total and a Place order button.
- Reserve: date, time, party size, name and phone.
WINDOWS: menu categories in a left rail, dish grid in the middle, and the cart panel on the right.

PALETTE OPTIONS: warm paprika #b91c1c on #fffbf5 with a golden #f59e0b accent; or deep olive #3f6212 on #fafaf5 with a cream surface. Serif headings and a friendly body font. Use large photos with rounded corners.

SIGNATURE FEATURES: veg and non-veg dots, spice level, "sold out" state controlled by the owner, today's special banner, opening hours with an open-now chip, WhatsApp order button, allergen note.

manage.json SECTIONS: dishes (name, price, category, veg, spicy, description, image, available), categories, offers (title, text, image), reservations and orders (inbox). 12 to 20 starter dishes across 5 categories.

SOURCES TO READ with read_url: Material Design 3 (m3.material.io) for chips, cards and bottom sheets; Fluent 2 (fluent2.microsoft.design) for the Windows layout; open-source restaurant UIs on GitHub with an MIT or Apache licence for the structure.
