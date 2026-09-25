/**
 * 每天台灣時間 10:30～22:30，每小時觸發一次 GitHub 上的「抓取價格」工作流程。
 *
 * 為什麼不用 GitHub 內建的排程：它在這個 repo 大部分時段直接被丟掉，
 * 跑的那幾次也晚好幾個小時。Apps Script 的定時觸發誤差在正負 15 分鐘內。
 *
 * 這個檔案不含任何密碼。用到的兩個值放在「專案設定 → 指令碼屬性」：
 *   GH_TOKEN  GitHub 的 fine-grained token（只給這個 repo 的 Actions 讀寫權限）
 *   GH_REPO   帳號/repo 名稱，例如 someone/some-repo
 *
 * 第一次使用：
 *   1. 設好上面兩個指令碼屬性
 *   2. 執行 testNow，到 GitHub 的 Actions 頁面確認出現一筆新的執行
 *   3. 執行 setup，建立每天 13 個定時觸發
 * 之後什麼都不用做。觸發失敗時 Google 會寄信通知。
 */

const WORKFLOW = 'track.yml';
const HOURS = [10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22];

/** 建立每天 13 個定時觸發。重複執行也安全：會先清掉舊的再建。 */
function setup() {
  for (const t of ScriptApp.getProjectTriggers()) {
    if (t.getHandlerFunction() === 'dispatch') ScriptApp.deleteTrigger(t);
  }
  for (const h of HOURS) {
    ScriptApp.newTrigger('dispatch')
      .timeBased()
      .atHour(h)
      .nearMinute(30)          // Apps Script 的精度是正負 15 分鐘
      .everyDays(1)
      .inTimezone('Asia/Taipei')
      .create();
  }
  Logger.log('已建立 %s 個定時觸發：台灣時間 %s 點各一次（約第 30 分）',
             HOURS.length, HOURS.join('、'));
}

/** 手動測試用：立刻觸發一次。 */
function testNow() {
  dispatch();
  Logger.log('已送出。到 GitHub 的 Actions 頁面，應該會看到一筆新的「抓取價格」。');
}

/** 叫 GitHub 立刻跑一次抓取。跟在網頁上按 Run workflow 是同一件事。 */
function dispatch() {
  const props = PropertiesService.getScriptProperties();
  const token = props.getProperty('GH_TOKEN');
  const repo = props.getProperty('GH_REPO');
  if (!token || !repo) {
    throw new Error('還沒設定指令碼屬性 GH_TOKEN 或 GH_REPO（專案設定 → 指令碼屬性）');
  }

  const url = 'https://api.github.com/repos/' + repo +
              '/actions/workflows/' + WORKFLOW + '/dispatches';
  const res = UrlFetchApp.fetch(url, {
    method: 'post',
    contentType: 'application/json',
    headers: {
      Authorization: 'Bearer ' + token,          // token 只放在標頭，不會出現在任何紀錄裡
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
    payload: JSON.stringify({ ref: 'main', inputs: { rollback: '' } }),
    muteHttpExceptions: true,
  });

  const code = res.getResponseCode();
  if (code < 200 || code >= 300) {
    // 丟出錯誤，Google 就會寄「觸發失敗」的通知信
    const hint = code === 401 ? '（token 錯誤或已過期，請重建一把並更新 GH_TOKEN）'
               : code === 404 ? '（GH_REPO 寫錯，或 token 沒有勾選這個 repo）'
               : code === 403 ? '（token 缺少 Actions 的讀寫權限）' : '';
    throw new Error('GitHub 回應 ' + code + hint + '：' + res.getContentText().slice(0, 200));
  }
}
