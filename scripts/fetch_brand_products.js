#!/usr/bin/env node
const puppeteer = require('puppeteer');

const wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function autoScroll(page, maxIterations = 10) {
  let previousCount = 0;
  for (let i = 0; i < maxIterations; i += 1) {
    await page.evaluate(() => {
      window.scrollBy(0, window.innerHeight);
    });
    await wait(1500);
    const currentCount = await page.evaluate(() => document.querySelectorAll('a[data-spm="n"]').length);
    if (currentCount === previousCount) {
      break;
    }
    previousCount = currentCount;
  }
}

async function extractProducts(page) {
  return page.evaluate(() => {
    function parseStockValueInside(rawText) {
      if (!rawText) {
        return null;
      }
      let text = String(rawText).replace(/[+,]/g, '').trim().toUpperCase();
      if (!text || text === '--') {
        return 0;
      }
      let multiplier = 1;
      if (text.endsWith('PCS')) {
        text = text.slice(0, -3);
      }
      if (text.endsWith('K')) {
        multiplier = 1_000;
        text = text.slice(0, -1);
      } else if (text.endsWith('M')) {
        multiplier = 1_000_000;
        text = text.slice(0, -1);
      } else if (text.endsWith('W')) {
        multiplier = 10_000;
        text = text.slice(0, -1);
      }
      const value = parseFloat(text);
      if (Number.isNaN(value)) {
        return null;
      }
      return Math.round(value * multiplier);
    }

    const cards = Array.from(document.querySelectorAll('section[class*="MainCard"], section[class*="maincard"], section.style__MainCard-sc-22zm6a-1'));
    return cards.map((card) => {
      const nameAnchor = card.querySelector('a[data-spm="n"]');
      const nameSpan = nameAnchor ? nameAnchor.querySelector('span') : null;
      const name = nameSpan ? nameSpan.textContent.trim() : nameAnchor ? nameAnchor.textContent.trim() : '';
      const productLink = nameAnchor ? nameAnchor.href : '';

      const imageAnchor = card.querySelector('a[data-custom-data]');
      let productCode = null;
      if (imageAnchor) {
        const datasetJson = imageAnchor.getAttribute('data-custom-data');
        if (datasetJson) {
          try {
            const parsed = JSON.parse(datasetJson);
            if (parsed && typeof parsed.productCode === 'string') {
              productCode = parsed.productCode;
            }
          } catch (error) {
            // ignore JSON parse errors
          }
        }
      }

      const propertyMap = {};
      card.querySelectorAll('dl').forEach((dl) => {
        const dt = dl.querySelector('dt');
        const dd = dl.querySelector('dd');
        const key = dt ? dt.textContent.trim() : null;
        const value = dd ? dd.textContent.trim() : null;
        if (!key && value && !propertyMap.__name) {
          propertyMap.__name = value;
        } else if (key && value) {
          propertyMap[key] = value;
        }
      });

      const totalStockMatch = card.innerText.match(/嘉立创库存\s*([\S]+)/);
      const totalStockText = totalStockMatch ? totalStockMatch[1].trim() : null;
      const totalStock = parseStockValueInside(totalStockText);

      const warehouses = Object.keys(propertyMap)
        .filter((key) => /仓$/.test(key))
        .map((key) => ({
          warehouse: key,
          stockText: propertyMap[key],
          stock: parseStockValueInside(propertyMap[key])
        }));

      const brand = propertyMap['品牌'] || null;
      const category = propertyMap['类目'] || null;

      const hasStock = (typeof totalStock === 'number' && totalStock > 0)
        || warehouses.some((item) => typeof item.stock === 'number' && item.stock > 0);

      return {
        name,
        link: productLink ? productLink.split('?')[0] : '',
        productCode: productCode || propertyMap['编号'] || null,
        brand,
        category,
        stockText: totalStockText,
        totalStock,
        warehouses,
        hasStock
      };
    }).filter((item) => item.name && item.link);
  });
}

async function main() {
  const targetUrl = process.argv[2];
  if (!targetUrl) {
    console.error(JSON.stringify({ error: 'Missing brand URL argument' }));
    process.exit(1);
  }

  const launchOptions = { headless: 'new', args: ['--no-sandbox', '--disable-setuid-sandbox'] };
  let browser;
  try {
    browser = await puppeteer.launch(launchOptions);
    const page = await browser.newPage();
    await page.setViewport({ width: 1280, height: 800 });
    page.setDefaultNavigationTimeout(90000);
    await page.goto(targetUrl, { waitUntil: 'networkidle0' });
    await wait(2000);
    await autoScroll(page, 12);
    const products = await extractProducts(page);
    await browser.close();

    const normalized = products.map((item) => ({
      name: item.name,
      link: item.link,
      productCode: item.productCode,
      brand: item.brand,
      category: item.category,
      stockText: item.stockText,
      totalStock: typeof item.totalStock === 'number' ? item.totalStock : null,
      warehouses: item.warehouses,
      hasStock: item.hasStock === true
    }));

    console.log(JSON.stringify({ products: normalized }, null, 2));
  } catch (error) {
    if (browser) {
      await browser.close();
    }
    console.error(JSON.stringify({ error: error.message || String(error) }));
    process.exit(1);
  }
}

main();
