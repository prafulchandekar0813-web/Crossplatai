---
name: playbook-delivery
description: Playbook for delivery apps (grocery, quick commerce, food, pharmacy, parcel): screens, navigation, palette options, signature features, manage.json sections, sources to read.
---
Use this together with app-design. It is a starting point to study and adapt, not a template to copy.

WHO IT IS FOR: busy people who want to find an item in seconds, see when it arrives, and reorder without thinking.

SCREENS (Android, bottom tabs): Home, Categories, Search, Cart, Orders (+ Profile or Help).
- Home: a location and delivery-time chip at the top ("Delivery in 12 min to Home"), a search bar, a promo banner carousel, category icon grid (8 to 12 tiles with icons), "Order again", "Best sellers", "Deals", each a horizontal scroll row of product cards.
- Product card: picture, name, weight or size, price, strike-through old price, an ADD button that turns into a - 1 + stepper in place.
- Product sheet (bottom sheet): bigger picture, description, size options, add to cart.
- Cart: items with steppers, bill summary (items, delivery fee, savings, total), a progress bar to free delivery, one main "Place order" button fixed at the bottom.
- Orders: status timeline (placed, packed, on the way, delivered) with a clear current step.
WINDOWS: a side rail (Home, Categories, Orders, Help), search on top, product grid of 4 to 6 columns, and the cart as a panel on the right that is always visible.

PALETTE OPTIONS (pick one, adapt the hex values, never copy a real brand's colours): fresh green #15803d on #f7fbf5 with an amber #f59e0b accent for deals; or deep violet #5b21b6 on #faf7ff with a lime #84cc16 accent for savings. Use round corners (14 to 18px) and a rounded sans font stack.

SIGNATURE FEATURES that make it feel real: a delivery-time promise, "Order again" row, a free-delivery progress bar, substitutions note, a sticky cart bar showing item count and total, empty and loading states with skeleton cards.

manage.json SECTIONS: products (name, price, old price, unit, category, in stock, image), categories (name, icon), banners (title, image, link), orders (inbox). Put 12 to 20 starter products across 6 categories.

SOURCES TO READ with read_url (prefer these): Material Design 3 guidance for navigation bar, cards, bottom sheets and search (m3.material.io); Baymard Institute's articles on cart and checkout usability (baymard.com/blog); the Fluent 2 design system for the Windows layout (fluent2.microsoft.design). Also search GitHub for open-source grocery or food-delivery UIs with an MIT or Apache licence and study their screen lists.
