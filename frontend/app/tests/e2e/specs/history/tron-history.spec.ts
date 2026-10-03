import type { FixtureBlockchainAccount } from '../../pages/types';
import { expect, type Locator } from '@playwright/test';
import { Blockchain } from '@rotki/common';
import { cleanupContext, createLoggedInContext, type SharedTestContext, test } from '../../fixtures/test-fixtures';
import { waitForNoRunningTasks } from '../../helpers/api';
import { TIMEOUT_MEDIUM } from '../../helpers/constants';
import { apiDecodeTransactions } from '../../helpers/history-events-api';
import { seedTronTransaction } from '../../helpers/seed-db';
import { BlockchainAccountsPage } from '../../pages/blockchain-accounts-page';
import { EVENT_ROW } from '../../pages/history-event-rows';
import { HistoryEventsPage } from '../../pages/history-events-page';

/**
 * rotkibv #5: the TRON account and history flows of the UI, offline. TronScan is never queried: the
 * account is added without a key, and its transactions are seeded as a sync stores them and decoded
 * by the real decoder through the API. The addresses are synthetic.
 */
const account: FixtureBlockchainAccount = {
  address: 'TQhqRHgEonKEYqudomS8243o3bejg8dt1d',
  blockchain: Blockchain.TRON,
  chainName: 'TRON',
  inputMode: 'manual_add',
  label: 'TRON 1',
  tags: [],
};

const SEND_TX = 'a1'.repeat(32);
const RECEIVE_TX = 'b2'.repeat(32);

test.describe.serial('tron history', () => {
  let ctx: SharedTestContext;
  let page: HistoryEventsPage;

  /** An event row by words of its notes. The amounts in notes render formatted, so they are not matched. */
  function row(notes: string): Locator {
    return ctx.sharedPage.locator(EVENT_ROW).filter({ hasText: notes });
  }

  /** The TRX transfer: its notes start with Send, as the fee type label may too. */
  function sendRow(): Locator {
    return row('Send').filter({ hasNotText: 'for gas' });
  }

  /** The group header of a transaction, found by its hash. */
  function group(hash: string): Locator {
    return ctx.sharedPage.locator('[data-testid=history-event-group]').filter({ hasText: hash.slice(0, 4) });
  }

  test.beforeAll(async ({ browser, request }) => {
    ctx = await createLoggedInContext(browser, request, {
      disableModules: true,
      seed: (username) => {
        seedTronTransaction(username, {
          account: account.address,
          hash: SEND_TX,
          id: 1,
          native: { amount: '1500000', fee: '268000', from: account.address, to: 'TSG5MMxrio1h8wT7D4XGz4XuxJGXwPhZBj' },
          timestamp: 1700000000000,
        });
        seedTronTransaction(username, {
          account: account.address,
          hash: RECEIVE_TX,
          id: 2,
          timestamp: 1700000060000,
          usdt: { amount: '25000000', from: 'TTpKHSFUdoi9j2zacMcRx522rztL61ojFS', to: account.address },
        });
      },
    });
    page = new HistoryEventsPage(ctx.sharedPage);
  });

  test.afterAll(async () => {
    await cleanupContext(ctx);
  });

  test('adds a TRON account without a TronScan key', async () => {
    const accounts = new BlockchainAccountsPage(ctx.sharedPage);
    await accounts.visit('tron');
    await accounts.openAddDialog();
    await accounts.addAccount(account);
    await accounts.isEntryVisible(0, account);
  });

  test('shows the decoded fee and transfers of the account', async ({ request }) => {
    // The page's own refresh stops at the missing key before decoding, so decode explicitly.
    await apiDecodeTransactions(request, 'tron');
    await page.visit();
    await page.applyTableFilter('location', 'tron');

    for (const events of [row('for gas'), sendRow(), row('Receive')])
      await expect(events).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });
  });

  test('keeps an edited event when its transaction is redecoded', async () => {
    await page.rows.edit(sendRow());
    await ctx.sharedPage.locator('[data-testid=notes] textarea:not([aria-hidden="true"])').fill('Paid the supplier');
    await page.saveForm();
    await expect(row('Paid the supplier')).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });

    await group(SEND_TX).locator('[data-testid=history-event-group-menu]').click();
    await ctx.sharedPage.getByRole('button', { name: 'Redecode events' }).click();
    await ctx.sharedPage.getByRole('button', { name: 'Proceed' }).click();
    await waitForNoRunningTasks(ctx.sharedPage);

    // a customized transaction is kept whole: no second fee, the edit stays
    await expect(row('Paid the supplier')).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });
    await expect(row('for gas')).toHaveCount(1);
  });

  test('excludes a transaction from accounting and deletes another', async () => {
    await group(SEND_TX).locator('[data-testid=history-event-group-menu]').click();
    await ctx.sharedPage.getByRole('button', { name: 'Exclude from Accounting' }).click();
    await expect(group(SEND_TX).locator('[data-testid=ignored-in-accounting]')).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });

    await group(RECEIVE_TX).locator('[data-testid=history-event-group-menu]').click();
    await ctx.sharedPage.getByRole('button', { name: 'Delete transaction & events' }).click();
    await ctx.sharedPage.locator('[data-testid=confirm-dialog] [data-testid=button-confirm]').click();
    await expect(row('Receive')).toHaveCount(0, { timeout: TIMEOUT_MEDIUM });
  });

  test('keeps the changes after logging in again', async () => {
    await ctx.app.relogin(ctx.username);
    await page.visit();
    await page.applyTableFilter('location', 'tron');

    await expect(row('Paid the supplier')).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });
    await expect(group(SEND_TX).locator('[data-testid=ignored-in-accounting]')).toHaveCount(1);
    await expect(row('Receive')).toHaveCount(0);
  });
});
