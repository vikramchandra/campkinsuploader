Campkins Batch Uploader
=======================

Installing
----------
You should have received CampkinsUploaderSetup.exe. Run it, click through
the wizard, and launch the app from the Start menu or desktop icon.

If Windows SmartScreen shows "Windows protected your PC", click
"More info" then "Run anyway". The app is safe; it is simply not
signed with a paid certificate.

If you received a zip instead: BEFORE extracting, right-click the zip,
open Properties, tick "Unblock" and click OK. Then extract anywhere and
run CampkinsBatchUploader.exe. Skipping the Unblock step makes Windows
silently refuse to start parts of the app.

Setting up
----------
Open Settings inside the app and fill in:
- Site URL, WordPress user and application password (used to upload
  images).
- WooCommerce key and secret (used to create products). These are not
  the same as the WordPress password.
- Store units: the weight and dimension units the site uses under
  WooCommerce > Settings > Products. The sheet always uses grams and
  millimetres; the app converts on upload.
- OpenRouter API key, for generating SEO meta with AI.
- Proxy (optional): pick a provider such as DataImpulse and enter the
  login and password from that provider's dashboard. Press "Test proxy"
  before saving. Use this when supplier sites block the scraper. Leave
  the provider on "None" to scrape from this computer's own connection.

If you were given a settings JSON file, use "Upload settings JSON" on
the Settings screen instead of typing everything in. Keep that file
private; it contains passwords and keys.

Using the app
-------------
- Start a run by choosing a CSV or Excel sheet. Needed columns: Product
  Name and Manufacturer URL. Also read if present: SKU, Price, EAN, Brand,
  Categories, Weight, Dimensions, Description, URL Slug, Meta title,
  Meta description.
- The app scrapes every image from each product page. Review each product,
  click images to keep or drop them, pick a thumbnail, then confirm and
  upload.
- To upload in bulk: on each product's confirm screen click "Mark ready
  to upload", then on the run overview click "Upload all ready".
- Everything is created as a draft on the site. Nothing is published.

Notes
-----
- Your work is saved automatically and survives closing the app.
- Images land in the output folder shown in Settings.
