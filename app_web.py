from flask import Flask, request, render_template_string, jsonify
import pandas as pd
import os
import unicodedata
from urllib.parse import unquote

app = Flask(__name__)

EXCEL_FILE = "bouquets.xlsx"
SHEET_NAME = "CRM"

# заказы в памяти
orders = []
next_order_id = 1

# --------- утилиты ----------
def norm(s):
    """Нормализация: убирает NBSP, лишние пробелы, приводим к нижнему регистру, ё->е"""
    if s is None:
        return ""
    s = str(s)
    s = s.replace('\xa0', ' ')
    s = s.strip()
    s = unicodedata.normalize('NFKC', s)
    s = s.replace('ё', 'е').replace('Ё', 'Е')
    s = ' '.join(s.split())
    return s.lower()

def load_data():
    """
    Читает EXCEL_FILE sheet SHEET_NAME и возвращает:
      bouquets: { normalized_bouquet_name: { flower_column_label: qty, ... }, ... }
      inventory: { flower_column_label: qty, ... }
    """
    if not os.path.exists(EXCEL_FILE):
        return {}, {}

    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME, engine="openpyxl", dtype=object)

    cols = list(df.columns)
    if len(cols) < 2:
        return {}, {}

    name_col = cols[0]
    flower_cols = cols[1:]

    # найти строку "склад" в колонке name_col (независимо от регистра/пробелов)
    mask = df[name_col].astype(str).fillna('').map(lambda x: norm(x) == 'склад')
    sklad_rows = df[mask].index
    if len(sklad_rows) == 0:
        # попробуем частичное вхождение
        mask2 = df[name_col].astype(str).fillna('').map(lambda x: 'склад' in norm(x))
        sklad_rows = df[mask2].index
        if len(sklad_rows) == 0:
            # не нашли строку склад — вернём пустые структуры
            return {}, {}
    sklad_row = sklad_rows[0]

    # читаем букеты (все строки до sklad_row)
    bouquets = {}
    for idx in df.index:
        if idx >= sklad_row:
            break
        raw_name = df.at[idx, name_col]
        if pd.isna(raw_name):
            continue
        b_name = norm(raw_name)
        if b_name == "":
            continue
        comp = {}
        for col in flower_cols:
            val = df.at[idx, col]
            if pd.notna(val):
                try:
                    q = int(val)
                except:
                    continue
                if q > 0:
                    comp[str(col)] = q
        if comp:
            bouquets[b_name] = comp

    # читаем склад из строки sklad_row
    inventory = {}
    for col in flower_cols:
        val = df.at[sklad_row, col]
        if pd.notna(val):
            try:
                inventory[str(col)] = int(val)
            except:
                pass

    return bouquets, inventory

def save_inventory(inventory):
    """Записывает inventory (keys — реальные заголовки столбцов цветов) в строку 'склад' Excel."""
    if not os.path.exists(EXCEL_FILE):
        raise FileNotFoundError("Excel файл не найден")

    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME, engine="openpyxl", dtype=object)
    cols = list(df.columns)
    if len(cols) < 2:
        raise ValueError("Неправильный формат Excel: мало колонок")
    name_col = cols[0]
    flower_cols = cols[1:]

    # найти строку "склад"
    mask = df[name_col].astype(str).fillna('').map(lambda x: norm(x) == 'склад')
    sklad_rows = df[mask].index
    if len(sklad_rows) == 0:
        mask2 = df[name_col].astype(str).fillna('').map(lambda x: 'склад' in norm(x))
        sklad_rows = df[mask2].index
        if len(sklad_rows) == 0:
            raise ValueError("Не найдена строка 'склад' при сохранении")
    sklad_row = sklad_rows[0]

    # записываем значения
    for col in flower_cols:
        key = str(col)
        if key in inventory:
            df.at[sklad_row, col] = int(inventory[key])

    # сохраняем лист
    df.to_excel(EXCEL_FILE, sheet_name=SHEET_NAME, index=False, engine="openpyxl")


# ---------- помощники для заказов ----------
def recompute_order_summary(order):
    """Пересчитать order['состав'] и order['букет'] по order['букеты'].""" 
    total = {}
    if 'букеты' in order:
        for b in order['букеты']:
            for f, q in b['состав'].items():
                total[f] = total.get(f, 0) + q
        order['состав'] = total
        order['букет'] = ", ".join([b.get('название', '') for b in order['букеты']])
    else:
        # если старый формат — ничего не трогаем
        pass
    return order

def ensure_order_buckets(order):
    """Если у заказа нет ключа 'букеты', создаём его из старого формата (для совместимости)."""
    if 'букеты' not in order:
        name = order.get('букет', '')
        comp = order.get('состав', {}) or {}
        order['букеты'] = [{"название": name, "состав": comp.copy()}]
    return order


# --------- HTML (UI) ----------
HTML = '''
<!doctype html>
<title>CRM салона</title>
<meta charset="utf-8">
<style>
  body { font-family: Arial, sans-serif; }
  table { border-collapse: collapse; width:100%; }
  th, td { border: 1px solid #444; padding: 6px; text-align: left; vertical-align: top; }
  .error { color: red; }
  .success { color: green; }
  .small { width: 70px; text-align: center; }
  .qty-cell { min-width: 40px; display: inline-block; padding:2px 4px; border-radius:3px; }
  .btn { padding:4px 8px; margin:2px; }
  #container { display:flex; gap:30px; margin-top:16px; align-items:flex-start; }
  .bouquet-block { margin-bottom:4px; }
  .comp-block + .comp-block { border-top:1px solid #eee; margin-top:6px; padding-top:6px; }
  .bouquet-block[contenteditable="true"] { outline: none; }
  .comp-block[contenteditable="true"] { outline: none; }
</style>

<h2>Проверить возможность сборки букета</h2>
<form id="checkForm">
  <input type="text" name="bouquet" placeholder="Название букета" autofocus>
  <input type="submit" value="Проверить" class="btn">
</form>

<div id="checkResult"></div>

<div id="container">
  <div style="flex:1;">
    <h2>Список заказов</h2>
    <table id="ordersTable">
      <tr>
        <th class="small">номер</th>
        <th>Букет</th>
        <th>Состав</th>
        <th>Статус</th>
        <th>Действие</th>
      </tr>
      {% for order in orders %}
      <tr data-index="{{ loop.index0 }}">
        <td class="small">
          <input type="number" class="orderNumber" value="{{ order['номер'] }}" style="width:60px;">
        </td>

        <!-- Букеты: каждый с новой строки; редактируемое имя каждого букета -->
        <td>
          {% if order.get('букеты') %}
            {% for b in order['букеты'] %}
              <div class="bouquet-block" contenteditable="true" data-bouquet-index="{{ loop.index0 }}">{{ b['название'] }}</div>
              {% if not loop.last %}<hr>{% endif %}
            {% endfor %}
          {% else %}
            <div class="bouquet-block" contenteditable="true" data-bouquet-index="0">{{ order.get('букет','') }}</div>
          {% endif %}
        </td>

        <!-- Состав: напротив каждого букета — его состав; каждый comp-block редактируем отдельно -->
        <td>
          {% if order.get('букеты') %}
            {% for b in order['букеты'] %}
              <div class="comp-block" data-bouquet-index="{{ loop.index0 }}" contenteditable="true">
                {% for f, q in b['состав'].items() %}
                  <div><span class="flower-name" contenteditable="false">{{ f }}</span>: <span class="qty-cell" contenteditable="true" data-flower="{{ f }}" data-bouquet-index="{{ loop.index0 }}">{{ q }}</span></div>
                {% endfor %}
                {% if b.get('shortage_text') %}
                 <div style="margin-top:6px; color:#a00; font-size:13px;">
                   {{ b['shortage_text'] }}
                 </div>
               {% endif %}
                {% if b.get('replacements') or b.get('with_replacement') %}
                  <div style="margin-top:6px;"><b>Замены (ручная правка)</b></div>
                  {% for r in b.get('replacements', []) %}
                    <div>{{ r['flower'] }}: {{ r['qty'] }}</div>
                  {% endfor %}
                {% endif %}
              </div>
              {% if not loop.last %}<hr>{% endif %}
            {% endfor %}
          {% else %}
            {% for f, q in order.get('состав',{}).items() %}
              <div><span class="flower-name" contenteditable="false">{{ f }}</span>: <span class="qty-cell" contenteditable="true" data-flower="{{ f }}" data-bouquet-index="0">{{ q }}</span></div>
            {% endfor %}
          {% endif %}
        </td>

        <td>
  <select class="orderStatus" data-index="{{ loop.index0 }}">
    <option value="забронировано" {% if order['статус']=="забронировано" %}selected{% endif %}>забронировано</option>
    <option value="отменен, не собран" {% if order['статус']=="отменен, не собран" %}selected{% endif %}>отменен, не собран</option>
    <option value="отменен, собран" {% if order['статус']=="отменен, собран" %}selected{% endif %}>отменен, собран</option>
    <option value="оплачен, собран" {% if order['статус']=="оплачен, собран" %}selected{% endif %}>оплачен, собран</option>
<option value="оплачен, не собран" {% if order['статус']=="оплачен, не собран" %}selected{% endif %}>оплачен, не собран</option>
  </select>
</td>
        <td><button class="deleteBtn btn">Удалить</button></td>
      </tr>
      {% endfor %}
    </table>
  </div>

  <div style="width:320px;">
    <h2>Остатки на складе</h2>
    <table id="inventoryTable" width="100%">
      <tr><th>Цветок</th><th>Кол-во</th></tr>
      {% for f, q in inventory.items() %}
      <tr>
        <td>{{ f }}</td>
        <td class="inv-edit" contenteditable="true">{{ q }}</td>
      </tr>
      {% endfor %}
    </table>
  </div>
</div>

<script>

window.tempOrder = window.tempOrder || [];
window.currentReplacements = window.currentReplacements || [];
window._lastInventory = window._lastInventory || {};

document.addEventListener('DOMContentLoaded', function() {
  const checkForm = document.getElementById('checkForm');
  const checkResultDiv = document.getElementById('checkResult');

let tempOrder = [];
window.tempOrder = tempOrder; 

  checkForm.addEventListener('submit', function(e){
    e.preventDefault();
    const formData = new FormData(checkForm);
    fetch('/check', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({
    bouquet: document.querySelector('[name="bouquet"]').value,
    tempOrder: window.tempOrder || []
  })
})
      .then(r => r.json())
      .then(data => {
        // сохраняем для возможного отображения замен
        window._lastInventory = data.остатки || {};
        let html = `<p><b>${data.букет}</b></p><p>Состав: `;
        for (let f in data.состав) html += `${f}: ${data.состав[f]} `;
        html += `</p>`;
        html += `<p class="${data.статус==='возможно'?'success':'error'}">${data.сообщение}</p>`;
        if (data.статус === 'возможно') {
          html += `<button class="btn bookNowBtn" data-bouquet="${(data.букет||'').replace(/"/g,'&quot;')}">Забронировать</button> `;
          html += `<button class="btn addBtn" data-bouquet="${(data.букет||'').replace(/"/g,'&quot;')}">Добавить в заказ</button>`;
          // добавляем кнопку "Добавить с заменой" — пометка, замена будет вручную в таблице
          html += ` <button class="btn replacementBtn" data-bouquet="${(data.букет||'').replace(/"/g,'&quot;')}">Добавить с заменой в заказ</button>`;
        } else {
          // при ошибке (недостаток) всё равно показываем кнопку "Собрать с заменой" (альтернатива)
          html += `<button class="btn replacementBtn" data-bouquet="${(data.букет||'').replace(/"/g,'&quot;')}">Добавить с заменой в заказ</button>`;
        }

        // показать остатки для удобства
        html += `<details style="margin-top:8px"><summary>Остатки</summary><pre style="white-space:pre-wrap;">${JSON.stringify(data.остатки || {}, null, 2)}</pre></details>`;
        checkResultDiv.innerHTML = html;
      }).catch(err=>{
        console.error(err);
        alert('Ошибка при проверке. Смотри консоль.');
      });
  });

  // добавление обычного букета в tempOrder (клиентская корзина)
  window.addToTemp = function(name){
    const fd = new FormData();
    fd.append('bouquet', name);
    fetch('/check', {method:'POST', body: fd})
      .then(r => r.json())
      .then(data => {
        if (data.статус !== 'возможно') {
          alert(data.сообщение || 'Нельзя добавить этот букет');
          return;
        }
        window.tempOrder = window.tempOrder || [];
        window.tempOrder.push({название: data.букет, состав: data.состав, with_replacement: FalseIfMissing}); // placeholder - will be corrected below
      }).then(()=> {
        // render after pushing (we push with real object below to avoid NameError)
        // ensure renderTemp exists
        if (typeof window.renderTemp === 'function') window.renderTemp();
      }).catch(err=>{ console.error(err); alert('Ошибка при добавлении в заказ'); });
  }

  // NOTE: previous line included a placeholder boolean; replace push with a safe implementation:
  // safer addToTemp implementation:
  window.addToTemp = function(name){
    const fd = new FormData();
    fd.append('bouquet', name);
    fetch('/check', {method:'POST', body: fd})
      .then(r => r.json())
      .then(data => {
        if (data.статус !== 'возможно') {
          alert(data.сообщение || 'Нельзя добавить этот букет');
          return;
        }
        window.tempOrder = window.tempOrder || [];
        window.tempOrder.push({название: data.букет, состав: data.состав, with_replacement: false});
        if (typeof window.renderTemp === 'function') window.renderTemp();
      }).catch(err=>{ console.error(err); alert('Ошибка при добавлении в заказ'); });
  }

  // render temporary order
  function renderTemp() {
    if (!window.tempOrder || window.tempOrder.length === 0) {
      checkResultDiv.innerHTML = '';
      return;
    }

    let html = `<p><b>Текущий заказ:</b></p><ul>`;
    window.tempOrder.forEach((o, i) => {
      if (typeof o === 'string') {
        html += `<li>${o} <button onclick="removeTemp(${i})">×</button></li>`;
      } else {
        html += `<li><b>${o['название'] || o['букет'] || ''}</b> <button onclick="removeTemp(${i})">×</button><br>`;
        for (let f in (o['состав']||{})) {
          html += `${f}: ${o['состав'][f]}<br>`;
        }
        if (o.with_replacement) html += `<i> (с заменой)</i>`;
        html += `</li>`;
      }
    });
    html += `</ul><button class="btn" onclick="finalizeBatch()">Завершить заказ</button> <button class="btn" onclick="clearTemp()">Отмена</button>`;
    checkResultDiv.innerHTML = html;
  }
  window.renderTemp = renderTemp;

  window.removeTemp = function(i){
    window.tempOrder.splice(i,1);
    renderTemp();
  }

  window.clearTemp = function(){
    window.tempOrder = [];
    checkResultDiv.innerHTML = '';
  }

  window.bookSingle = function(name){
    const fd = new FormData();
    fd.append('bouquet', name);
    fetch('/book', {method:'POST', body: fd}).then(()=> location.reload());
  }

  window.finalizeBatch = function(){
    if (!window.tempOrder || window.tempOrder.length === 0) return;
    fetch('/book_batch', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({bouquets: window.tempOrder})
    }).then(resp => {
      if (resp.ok) location.reload();
      else resp.json().then(j => alert(j.error || 'Ошибка при бронировании'));
    }).catch(err=>{ console.error(err); alert('Ошибка при финализации'); });
  }

  // delete
  document.querySelectorAll('.deleteBtn').forEach(btn=>{
    btn.addEventListener('click', function(){
      const index = this.closest('tr').dataset.index;
      fetch(`/delete/${index}`, {method:'POST'}).then(()=> location.reload());
    });
  });

  // order number edit
  document.querySelectorAll('.orderNumber').forEach(input=>{
    input.addEventListener('blur', function(){
      const tr = this.closest('tr');
      const index = tr.dataset.index;
      const new_num = this.value;
      fetch(`/edit_order_number/${index}`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({new_num})
      });
    });
  });

  // редактирование имени каждого букета (при наличии нескольких)
  document.querySelectorAll('.bouquet-block').forEach(div=>{
    div.addEventListener('blur', function(){
      const tr = this.closest('tr');
      const index = tr.dataset.index;
      const bidx = this.dataset.bouquetIndex || 0;
      const new_name = this.innerText.trim();
      fetch(`/edit_order/${index}`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({new_name: new_name, bouquet_idx: bidx})
      }).then(()=> location.reload());
    });
  });

  // редактирование состава вручную для конкретного букета (comp-block)
  document.querySelectorAll('.comp-block').forEach(block=>{
    block.dataset.orig = (block.innerText || "").trim();
    block.addEventListener('blur', function(){
      const tr = this.closest('tr');
      const index = tr.dataset.index;
      const bidx = this.dataset.bouquetIndex || 0;
      const lines = Array.from(this.querySelectorAll('div')).map(d=>d.innerText.trim()).filter(s=>s);
      const text = lines.length ? lines.join('\\n') : (this.innerText || "").trim();
      if (text === (this.dataset.orig || "")) return;
      fetch(`/edit_order_composition/${index}`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({bouquet_idx: bidx, composition: text})
      }).then(resp=>{
        if (!resp.ok) {
          resp.json().then(j => { alert(j.message || 'Ошибка при изменении состава'); location.reload(); });
        } else {
          location.reload();
        }
      }).catch(err => { console.error(err); alert('Ошибка запроса'); location.reload(); });
    });
  });

  // qty-cell (редактирование количества для конкретного букета)
  document.querySelectorAll('.qty-cell').forEach(span=>{
    span.addEventListener('blur', function(){
      const tr = this.closest('tr');
      const index = tr.dataset.index;
      const bouquet_idx = this.dataset.bouquetIndex || 0;
      const flower = this.dataset.flower;
      const new_qty = parseInt(this.innerText) || 0;
      fetch(`/edit_order_qty/${index}`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({flower, new_qty, bouquet_idx})
      }).then(resp => {
        if (!resp.ok) {
          alert('Недостаточно на складе — изменение отменено');
        }
        location.reload();
      }).catch(err => { console.error(err); alert('Ошибка запроса'); location.reload(); });
    });
  });

document.addEventListener('change', function(e) {
  if (e.target.classList.contains('orderStatus')) {
    const index = e.target.dataset.index;
    const status = e.target.value;

    fetch(`/edit_order_status/${index}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({status: status})
    });
  }
});

  // inventory edit
  document.querySelectorAll('.inv-edit').forEach(td=>{
    td.addEventListener('blur', function(){
      const tr = this.closest('tr');
      const flower = tr.children[0].innerText;
      const qty = parseInt(this.innerText) || 0;
      fetch(`/edit_inventory/${encodeURIComponent(flower)}`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({new_qty: qty})
      }).then(()=> location.reload());
    });
  });

}); // DOMContentLoaded

// Делегирование кликов для динамических кнопок: replacementBtn, addBtn, bookNowBtn
document.addEventListener('click', function(e){
  const t = e.target;
  if (t.matches('.replacementBtn')) {
    const bouquet = t.dataset.bouquet || t.getAttribute('data-bouquet');
    // Простое поведение: пометить как "с заменой" и добавить в tempOrder
    addReplacementToTempSimple(bouquet);
    return;
  }
  if (t.matches('.addBtn')) {
    const bouquet = t.dataset.bouquet || t.getAttribute('data-bouquet');
    if (typeof window.addToTemp === 'function') window.addToTemp(bouquet);
    return;
  }
  if (t.matches('.bookNowBtn')) {
    const bouquet = t.dataset.bouquet || t.getAttribute('data-bouquet');
    if (typeof window.bookSingle === 'function') window.bookSingle(bouquet);
    return;
  }
  if (e.target.classList.contains('replacementBtn')) {
  const bouquetName = (e.target.dataset.bouquet || '').trim();
  console.log('Добавляем букет с заменой:', bouquetName);

  // гарантируем, что tempOrder существует
  window.tempOrder = window.tempOrder || [];

  // приведение всех названий к единому виду (в нижний регистр)
  const baseName = bouquetName.toLowerCase();

  // считаем, сколько уже есть похожих букетов (с тем же baseName)
  const countSame = window.tempOrder.filter(b => {
    if (typeof b === 'string') {
      return b.toLowerCase().startsWith(baseName);
    } else if (b && b.название) {
      return b.название.toLowerCase().startsWith(baseName);
    }
    return false;
  }).length;

  // формируем новое имя
  const displayName =
    countSame > 0
      ? `${bouquetName} (с заменой ${countSame + 1})`
      : `${bouquetName} (с заменой)`;

  // добавляем букет как объект
  window.tempOrder.push({
    название: displayName,
    состав: {},
    with_replacement: true
  });

  console.log('Теперь tempOrder:', window.tempOrder);

  // перерисовываем временный заказ
  if (typeof renderTemp === 'function') renderTemp();

  // очищаем блок проверки
  const cr = document.getElementById('checkResult');
  if (cr) cr.innerHTML = '';
}
});

window.addReplacementToTempSimple = function(bouquetName){
  console.log("addReplacementToTempSimple called:", bouquetName);

  window.tempOrder = window.tempOrder || [];

  fetch('/check', {
    method: 'POST',
    headers: {'Content-Type':'application/json'},
    body: JSON.stringify({ bouquet: bouquetName, tempOrder: window.tempOrder })
  })
  .then(r => r.json())
  .then(data => {
    if (!data) {
      alert('Ошибка: нет ответа от сервера');
      return;
    }
    const orig = data.состав || {};
    const leftovers = data.остатки || {};

    const actual = {};
    for (const f in orig) {
      const need = parseInt(orig[f]) || 0;
      const avail = parseInt(leftovers[f] || 0) || 0;
      const take = Math.max(0, Math.min(need, avail));
      if (take > 0) actual[f] = take;
    }

    if (Object.keys(actual).length === 0) {
      if (!confirm('Ни одного цветка из этого букета нельзя взять сейчас. Всё равно добавить помеченный как (с заменой) букет?')) {
        return;
      }
    }

    const last = window.tempOrder.length ? window.tempOrder[window.tempOrder.length - 1] : null;
    const expectedName = (bouquetName || '') + ' (с заменой)';
    if (last && String(last.название || '').toLowerCase() === String(expectedName).toLowerCase() && last.with_replacement) {
      last.состав = actual;
      if (typeof window.renderTemp === 'function') window.renderTemp();
      return;
    }

    window.tempOrder.push({
      название: expectedName,
      состав: actual,
      with_replacement: true
    });

    console.log('tempOrder after push (replacement):', window.tempOrder);

    if (typeof window.renderTemp === 'function') window.renderTemp();
  })
  .catch(err => {
    console.error('addReplacementToTempSimple error:', err);
    alert('Ошибка при добавлении букета с заменой. Смотри консоль.');
  });
};
</script>
'''

# --------- логика работы ------------
def check_order_with_data(name, bouquets, inventory):
    name_l = norm(name)

    if name_l not in bouquets:
        return {
            "букет": name,
            "состав": {},
            "статус": "ошибка",
            "сообщение": "Такого букета нет в базе"
        }

    recipe = bouquets[name_l].copy()
    missing = []

    for f, q in recipe.items():
        available = inventory.get(f, 0)
        if available < q:
            missing.append(f"{f} (нужно {q}, есть {available})")

    if missing:
        return {
            "букет": name,
            "состав": recipe,
            "статус": "ошибка",
            "сообщение": "Недостаточно:\n" + ", ".join(missing)
        }

    return {
        "букет": name,
        "состав": recipe,
        "статус": "возможно",
        "сообщение": "Возможно собрать"
    }


def book_order_with_data(bouquet_name, bouquets, inventory):
    """Бронирует один букет — создаёт заказ с 'букеты' (список из 1 элемента) + совместимые поля."""
    global next_order_id
    res = check_order_with_data(bouquet_name, bouquets, inventory)
    if res['статус'] != 'возможно':
        return None
    recipe = res['состав'].copy()
    # списываем
    for f, q in recipe.items():
        inventory[f] = inventory.get(f, 0) - q
    save_inventory(inventory)

    order = {
        "номер": next_order_id,
        "букеты": [
            {"название": bouquet_name, "состав": recipe.copy()}
        ],
        "букет": bouquet_name,
        "состав": recipe.copy(),
        "статус": "забронировано"
    }
    next_order_id += 1
    orders.insert(0, order)
    return order


@app.route("/")
def index():
    try:
        bouquets, inventory = load_data()
    except Exception:
        bouquets, inventory = {}, {}
    # Для отображения: убедимся, что у каждого заказа есть 'букеты' (для совместимости)
    for o in orders:
        ensure_order_buckets(o)
    return render_template_string(HTML, orders=orders, inventory=inventory)


@app.route("/check", methods=["POST"])
def check():
    try:
        bouquets, inventory = load_data()
    except Exception:
        return jsonify({"букет": "", "состав": {}, "статус": "ошибка", "сообщение": "Ошибка чтения Excel"}), 500

    # поддерживаем form-data (форма) и json (внешние вызовы)
    if request.is_json:
        data = request.get_json() or {}
        name = (data.get("bouquet") or "").strip()
        temp = data.get("tempOrder", [])
    else:
        name = request.form.get("bouquet", "").strip()
        temp = []

    # учтём временные заказы (temp) — уменьшим копию inventory
    inv_copy = inventory.copy()
    for item in temp:
        if isinstance(item, dict):
            comp = item.get('состав') or {}
            for f, q in comp.items():
                inv_copy[f] = inv_copy.get(f, 0) - int(q)
        else:
            key = norm(item)
            if key in bouquets:
                for f, q in bouquets[key].items():
                    inv_copy[f] = inv_copy.get(f, 0) - int(q)

    result = check_order_with_data(name, bouquets, inv_copy)
    result["остатки"] = inv_copy
    return jsonify(result)

@app.route("/apply_temp_inventory", methods=["POST"])
def apply_temp_inventory():
    global temp_inventory
    data = request.get_json() or {}
    comp = data.get("composition", {})
    for f, q in comp.items():
        temp_inventory[f] = temp_inventory.get(f, 0) - q
    return jsonify({"ok": True})

@app.route("/book", methods=["POST"])
def book():
    name = request.form.get("bouquet", "")
    try:
        bouquets, inventory = load_data()
    except:
        return '', 500
    book_order_with_data(name, bouquets, inventory)
    return '', 204

@app.route("/book_batch", methods=["POST"])
def book_batch():
    global next_order_id

    data = request.get_json() or {}
    items = data if isinstance(data, list) else data.get("bouquets", [])
    if not items:
        return jsonify({"error":"Пустой список букетов"}), 400

    try:
        bouquets, inventory = load_data()
    except Exception:
        return jsonify({"error":"Ошибка чтения Excel"}), 500

    prepared = []
    for it in items:
        if isinstance(it, dict):
            name = it.get('название') or it.get('букет') or 'без имени'
            comp = {str(k): int(v) for k, v in (it.get('состав') or {}).items()}
            with_repl = bool(it.get('with_replacement', False))
        else:
            name = it
            key = norm(name)
            if key not in bouquets:
                return jsonify({"error": f"Неизвестный букет: {name}"}), 400
            comp = bouquets[key].copy()
            with_repl = False
        prepared.append({"название": name, "состав": comp, "with_replacement": with_repl})

    total_needed = {}
    for p in prepared:
        if not p["with_replacement"]:
            for f, q in p["состав"].items():
                total_needed[f] = total_needed.get(f, 0) + int(q)
    
    for f, q in total_needed.items():
        if inventory.get(f, 0) < q:
            return jsonify({"error": f"Недостаточно {f} (осталось {inventory.get(f,0)})"}), 400
    for f, q in total_needed.items():
        inventory[f] = inventory.get(f, 0) - q

    for p in prepared:
        if p["with_replacement"]:
            comp = p.get("состав") or {}
            if not comp:
                key = norm(p["название"].replace(" (с заменой)", ""))  
                comp = bouquets.get(key, {}).copy()

    allocated = {}
    shortage = []

    for f, need in comp.items():
        need_i = int(need)
        avail = inventory.get(f, 0)

        take = min(need_i, avail) if need_i > 0 else 0

        allocated[f] = take

        inventory[f] = inventory.get(f, 0) - take
        if take < need_i:
            shortage.append(f"{f}: нужно {need_i}, есть {take}")


    p["состав"] = allocated
    if shortage:
        p["shortage_text"] = "; ".join(shortage)

    try:
        save_inventory(inventory)
    except Exception as e:
        return jsonify({"error": "Ошибка записи Excel: " + str(e)}), 500

    
    order = {
        "номер": next_order_id,
        "букеты": prepared,
        "букет": ", ".join([p['название'] for p in prepared]),
        "состав": {}, 
        "статус": "забронировано"
    }
    
    total = {}
    for p in prepared:
        for f, q in (p.get("состав") or {}).items():
            total[f] = total.get(f, 0) + int(q)
    order["состав"] = total

    next_order_id += 1
    orders.insert(0, order)
    return jsonify(order), 201

@app.route("/edit_order_number/<int:index>", methods=["POST"])
def edit_order_number(index):
    data = request.get_json() or {}
    try:
        new_num = int(data.get("new_num"))
    except:
        return '', 400
    if 0 <= index < len(orders):
        orders[index]['номер'] = new_num
        return '', 204
    return '', 400


@app.route("/delete/<int:index>", methods=["POST"])
def delete_order(index):
    if 0 <= index < len(orders):
        try:
            bouquets, inventory = load_data()
        except:
            inventory = {}
        # вернуть на склад: если есть 'букеты' — суммируем по всем, иначе старый формат
        if 'букеты' in orders[index]:
            for b in orders[index]['букеты']:
                for f, q in b['состав'].items():
                    inventory[f] = inventory.get(f, 0) + q
        else:
            for f, q in orders[index]['состав'].items():
                inventory[f] = inventory.get(f, 0) + q
        save_inventory(inventory)
        orders.pop(index)
    return '', 204


@app.route("/edit_order/<int:index>", methods=["POST"])
def edit_order(index):
    """Редактирование имени букета. Поддерживается bouquet_idx для редактирования конкретного букета внутри заказа."""
    data = request.get_json() or {}
    new_name = (data.get("new_name") or "").strip()
    try:
        bouquet_idx = int(data.get("bouquet_idx")) if data.get("bouquet_idx") is not None else None
    except:
        bouquet_idx = None

    if not (0 <= index < len(orders)):
        return '', 400

    order = orders[index]
    if 'букеты' in order:
        if bouquet_idx is None:
            if len(order['букеты']) == 1 and new_name:
                order['букеты'][0]['название'] = new_name
        else:
            if 0 <= bouquet_idx < len(order['букеты']) and new_name:
                order['букеты'][bouquet_idx]['название'] = new_name
        recompute_order_summary(order)
    else:
        if new_name:
            order['букет'] = new_name
    return '', 204


@app.route("/edit_inventory/<flower>", methods=["POST"])
def edit_inventory(flower):
    flower = unquote(flower)
    data = request.get_json()
    try:
        new_qty = int(data.get("new_qty"))
    except:
        return '', 400
    try:
        bouquets, inventory = load_data()
    except:
        inventory = {}
    inventory[flower] = new_qty
    save_inventory(inventory)
    return '', 204


@app.route("/edit_order_qty/<int:index>", methods=["POST"])
def edit_order_qty(index):
    """Редактирование количества конкретного цветка в конкретном букете заказа.
       Принимает JSON {flower, new_qty, bouquet_idx} (bouquet_idx optional, default 0)."""
    data = request.get_json() or {}
    flower = data.get("flower")
    try:
        new_qty = int(data.get("new_qty", 0))
    except:
        return '', 400
    try:
        bouquet_idx = int(data.get("bouquet_idx")) if data.get("bouquet_idx") is not None else 0
    except:
        bouquet_idx = 0

    if not (0 <= index < len(orders)):
        return '', 400

    try:
        bouquets, inventory = load_data()
    except:
        inventory = {}

    order = orders[index]
    # если есть 'букеты', работаем с указанным букетом
    if 'букеты' in order:
        if not (0 <= bouquet_idx < len(order['букеты'])):
            return '', 400
        comp = order['букеты'][bouquet_idx]['состав']
        if flower not in comp:
            return '', 400
        old_qty = comp[flower]
        diff = new_qty - old_qty
        if inventory.get(flower, 0) - diff < 0:
            return '', 400
        comp[flower] = new_qty
        # обновляем суммарный состав и записываем
        recompute_order_summary(order)
        inventory[flower] = inventory.get(flower, 0) - diff
        save_inventory(inventory)
        return '', 204
    else:
        # старый формат
        if flower not in order['состав']:
            return '', 400
        old_qty = order['состав'][flower]
        diff = new_qty - old_qty
        if inventory.get(flower, 0) - diff < 0:
            return '', 400
        order['состав'][flower] = new_qty
        inventory[flower] = inventory.get(flower, 0) - diff
        save_inventory(inventory)
        return '', 204


@app.route("/edit_order_composition/<int:index>", methods=["POST"])
def edit_order_composition(index):
    """
    Редактирование состава вручную. JSON: { bouquet_idx (optional), composition: "цвет: число\n..." }
    Изменяет состав конкретного букета (если есть 'букеты') или старого формата.
    """
    data = request.get_json() or {}
    new_text = data.get("composition", "")
    try:
        bouquet_idx = int(data.get("bouquet_idx")) if data.get("bouquet_idx") is not None else 0
    except:
        bouquet_idx = 0

    if not (0 <= index < len(orders)):
        return jsonify({"status":"ошибка","message":"Неверный индекс заказа"}), 400

    try:
        bouquets, inventory = load_data()
    except:
        return jsonify({"status":"ошибка","message":"Ошибка чтения Excel"}), 500

    # парсим строки вида "Цветок: число"
    new_comp = {}
    for line in new_text.splitlines():
        if ":" not in line:
            continue
        flower_raw, qty_raw = line.split(":", 1)
        flower = flower_raw.strip()
        try:
            qty = int(qty_raw.strip())
        except:
            continue
        if qty > 0:
            new_comp[flower] = qty

    order = orders[index]
    # работа для детального формата
    if 'букеты' in order:
        if not (0 <= bouquet_idx < len(order['букеты'])):
            return jsonify({"status":"ошибка","message":"Неверный индекс букета в заказе"}), 400
        old_comp = order['букеты'][bouquet_idx]['состав']
        # вернуть старый на склад
        for f, q in old_comp.items():
            inventory[f] = inventory.get(f, 0) + q
        # проверить новые
        for f, q in new_comp.items():
            if inventory.get(f, 0) < q:
                # откат возврата
                for f2, q2 in old_comp.items():
                    inventory[f2] = inventory.get(f2, 0) - q2
                return jsonify({"status":"ошибка","message":f"Недостаточно {f} (осталось {inventory.get(f,0)})"}), 400
        # списать новые
        for f, q in new_comp.items():
            inventory[f] = inventory.get(f, 0) - q
        order['букеты'][bouquet_idx]['состав'] = new_comp
        recompute_order_summary(order)
        save_inventory(inventory)
        return '', 204
    else:
        # старый формат
        old_comp = order['состав']
        for f, q in old_comp.items():
            inventory[f] = inventory.get(f, 0) + q
        for f, q in new_comp.items():
            if inventory.get(f, 0) < q:
                for f2, q2 in old_comp.items():
                    inventory[f2] = inventory.get(f2, 0) - q2
                return jsonify({"status":"ошибка","message":f"Недостаточно {f} (осталось {inventory.get(f,0)})"}), 400
        for f, q in new_comp.items():
            inventory[f] = inventory.get(f, 0) - q
        order['состав'] = new_comp
        save_inventory(inventory)
        return '', 204

@app.route("/edit_order_status/<int:index>", methods=["POST"])
def edit_order_status(index):
    data = request.get_json() or {}
    new_status = data.get("status")

    allowed = [
        "забронировано",
        "отменен, не собран",
        "отменен, собран",
        "оплачен, собран",
        "оплачен, не собран"
    ]

    if new_status not in allowed:
        return '', 400

    if not (0 <= index < len(orders)):
        return '', 400

    orders[index]["статус"] = new_status
    return '', 204


# диагностический маршрут
@app.route("/debug_data")
def debug_data():
    try:
        bouquets, inventory = load_data()
    except Exception as e:
        return jsonify({"error": str(e)})
    return jsonify({
        "bouquets_count": len(bouquets),
        "bouquet_names_sample": list(bouquets.keys())[:50],
        "inventory": inventory
    })


@app.route("/book_with_replacement", methods=["POST"])
def book_with_replacement():
    """
    Подготовить букет с заменой: вернуть объект {"название","состав","with_replacement": True}.
    НЕ списывать инвентарь и НЕ создавать заказ — это делается при finalize (/book_batch).
    """
    data = request.get_json() or {}
    name = (data.get("original_bouquet") or "").strip()
    replacements = data.get("replacements", [])

    bouquets, inventory = load_data()
    if norm(name) not in bouquets:
        return jsonify({"error": "Неизвестный букет"}), 400

    # исходный рецепт (копия)
    recipe = bouquets[norm(name)].copy()

    # применяем замены: только увеличиваем состав тем, что выбрал пользователь
    for repl in replacements:
        rf = repl.get('flower')
        try:
            rq = int(repl.get('qty', 0))
        except:
            rq = 0
        if rq > 0:
            recipe[rf] = recipe.get(rf, 0) + rq

    # возвращаем подготовленный букет (без списания, без создания заказа)
    return jsonify({
        "название": f"{name} (с заменой)",
        "состав": recipe,
        "with_replacement": True
    })


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)