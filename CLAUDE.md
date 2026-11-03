# Cakes Joséphine

Deux applis pour la production des cakes du Café Joséphine (Nice) :

- **Stock cakes** : l'équipe du café saisit, vers 17h-17h40, les parts restantes des 5 saveurs.
- **Cakes** : la page de Paula, qui indique les cakes à produire le soir à partir de ce stock et des ventes Square.

## Qui et comment

- Propriétaire : Jean-Charles. Répondre en français, simplement, sans jargon technique.
- Avant toute publication sur GitHub ou dans Google Apps Script, lui montrer ce qui change et attendre son accord.
- Quand une modification change ce qui est décrit ici, mettre cette note à jour dans le même envoi.

## Où ça tourne

- Dépôt GitHub `Josephine-Nice/cakes`, **public**. Le compte `Josephine-Nice` est connecté sur ce Mac (`gh`).
- **Cakes** : GitHub Actions (`.github/workflows/page.yml`) lance `production_cakes.py --html site/index.html`
  chaque nuit après minuit (la page repart au rouge), vers 15h30, puis toutes les 10 minutes de 17h à 19h (lun-ven, Paris).
  La page est publiée sur https://josephine-nice.github.io/cakes/
- Le robot `cakes-bot` met à jour `journal_production.json` (productions passées) : faire un `git pull` avant de modifier quoi que ce soit.
- **Stock cakes** : le raccourci de l'iPad pointe vers `stock.html` (sur GitHub Pages), qui redirige vers une appli web
  **Google Apps Script**. Le formulaire est `Index.html` et appelle `enregistrer()` côté serveur.
  Le code serveur (`Code.gs`) n'existe que dans l'éditeur Apps Script : toute modification se colle là-bas, puis se redéploie à la main.
  L'adresse `stock.html#installer` sert seulement à créer le raccourci avec la bonne icône.

## Données

- Ventes : API Square (Orders), commandes terminées. Jeton Square différent de celui de l'appli Cups.
- Stock saisi : Google Sheet publié en CSV, lien dans le secret `STOCK_SHEET_URL` (colonnes de `modele_stock.csv`).
- Secrets GitHub : `SQUARE_ACCESS_TOKEN`, `SQUARE_LOCATION_ID`, `STOCK_SHEET_URL`.
- Le script sait encore envoyer un SMS/WhatsApp via Twilio, mais le workflow ne s'en sert pas : Paula consulte la page.

## Règles métier (réglages en haut de `production_cakes.py`)

- 5 saveurs : Mel (marbré chocolat), Celia (citron), Elisa (carrot cake), Val (matcha chocolat blanc), Audrey (rose framboise).
- 11 parts par cake. Paula produit du lundi au vendredi (prise de poste à 17h30), au maximum 6 cakes par jour,
  5 visés (4 le vendredi pour préparer le week-end et le lundi). Le café est ouvert du lundi au samedi.
- Un cake produit le jour J se vend de J+1 à J+4 (J+3 pour Audrey et Val).
- Prévision : les mêmes jours de semaine sur les 4 dernières semaines, +10 %.

## Fichiers

- Publiés sur GitHub : `production_cakes.py`, `stock.html`, les icônes, `README.md`, `journal_production.json`,
  `.github/workflows/page.yml`, `modele_stock.csv`, et `Index.html` (copie de référence du formulaire Apps Script :
  après une modification, la recoller aussi dans l'éditeur Apps Script).
- Locaux seulement, jamais publiés (exclus par `.git/info/exclude`) : `.env` et `env.exemple`, qui contient un vrai numéro de téléphone.

## Précautions

- Le dépôt et les pages sont publics : jamais de jeton, de numéro de téléphone ni de lien de Sheet dans le code ou les fichiers publiés.
- `.env` contient les vrais secrets (Square, Twilio, numéros) : ne jamais le publier ni le recopier. Il est exclu par `.git/info/exclude` sur ce Mac.

## Autres applis Joséphine

- Dossier `~/josephine/cups` (dépôt `Josephine-Nice/cups`) : stock des cups à emporter et alerte de commande.
