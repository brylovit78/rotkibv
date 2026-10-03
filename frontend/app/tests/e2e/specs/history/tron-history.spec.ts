import type { FixtureBlockchainAccount } from '../../pages/types';
import { expect, type Locator } from '@playwright/test';
import { Blockchain } from '@rotki/common';
import { cleanupContext, createLoggedInContext, type SharedTestContext, test } from '../../fixtures/test-fixtures';
import { apiLogout } from '../../helpers/api';
import { TIMEOUT_MEDIUM } from '../../helpers/constants';
import { apiDecodeTransactions, apiHistoryEvents } from '../../helpers/history-events-api';
import { seedHistoricPrices, seedTronTransaction } from '../../helpers/seed-db';
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

const TRON_USDT = 'tron/trc20:41a614f803b6fd780986a42c78ec9c7f77e6ded13c';
const SEND_TX = { hash: 'a1'.repeat(32), timestamp: 1700000000 };
const RECEIVE_TX = { hash: 'b2'.repeat(32), timestamp: 1700000060 };

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

  /** One event with the amount and the asset of the seeded rows. */
  async function expectEvent(events: Locator, amount: string, asset: string): Promise<void> {
    await expect(events).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });
    await page.rows.expectAmount(events, amount);
    await expect(events.locator('[data-testid=event-asset]')).toContainText(asset);
  }

  /** The group header of a transaction, found by its hash. */
  function group(hash: string): Locator {
    return ctx.sharedPage.locator('[data-testid=history-event-group]').filter({ hasText: hash.slice(0, 4) });
  }

  test.beforeAll(async ({ browser, request }) => {
    ctx = await createLoggedInContext(browser, request, {
      disableModules: true,
      seed: (username) => {
        // without stored prices the rows and the edit form ask the price oracles of the network
        for (const { timestamp } of [SEND_TX, RECEIVE_TX])
          seedHistoricPrices([{ fromAsset: 'TRX', price: '0.1' }, { fromAsset: TRON_USDT, price: '1' }], timestamp);

        seedTronTransaction(username, {
          account: account.address,
          hash: SEND_TX.hash,
          id: 1,
          native: { amount: '1500000', fee: '270000', from: account.address, to: 'TSG5MMxrio1h8wT7D4XGz4XuxJGXwPhZBj' },
          timestamp: SEND_TX.timestamp * 1000,
        });
        seedTronTransaction(username, {
          account: account.address,
          hash: RECEIVE_TX.hash,
          id: 2,
          timestamp: RECEIVE_TX.timestamp * 1000,
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

    const accountRow = ctx.sharedPage.locator('[data-testid=account-table] tbody tr[data-id="row"]').filter({ hasText: account.label });
    await expect(accountRow).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });
    await accountRow.locator('[data-testid=labeled-address-display]').hover();
    await expect(ctx.sharedPage.locator('div[role=tooltip] [data-id=content]')).toContainText(account.address);
  });

  test('shows the decoded fee and transfers of the account', async ({ request }) => {
    // The page's own refresh stops at the missing key before decoding, so decode explicitly.
    expect(await apiDecodeTransactions(request, 'tron')).toBe(2);
    // exact amounts and asset identifiers; the rows below only show them formatted
    const events = await apiHistoryEvents(request, 'tron');
    expect(events).toHaveLength(3);
    expect(events).toEqual(expect.arrayContaining([
      ['spend', 'fee', 'TRX', '0.27'],
      ['spend', 'none', 'TRX', '1.5'],
      ['receive', 'none', TRON_USDT, '25'],
    ]));
    await page.visit();
    await page.applyTableFilter('location', 'tron');

    await expectEvent(row('for gas'), '0.27', 'TRX');
    await expectEvent(sendRow(), '1.5', 'TRX');
    await expectEvent(row('Receive'), '25', 'USDT');
  });

  async function editSendNotes(notes: string): Promise<void> {
    await page.rows.edit(sendRow());
    await ctx.sharedPage.locator('[data-testid=notes] textarea:not([aria-hidden="true"])').fill(notes);
    await page.saveForm();
    await expect(row(notes)).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });
  }

  test('regenerates a customized transaction when it is redecoded from its menu', async () => {
    await editSendNotes('Paid the supplier');

    // The dialog warns that a redecode removes the custom events, and Proceed confirms that.
    await group(SEND_TX.hash).locator('[data-testid=history-event-group-menu]').click();
    await ctx.sharedPage.getByRole('button', { name: 'Redecode events' }).click();
    await ctx.sharedPage.getByRole('button', { name: 'Proceed' }).click();

    // the decoded notes are back, and the transaction still has one fee and one transfer
    await expect(row('Paid the supplier')).toHaveCount(0, { timeout: TIMEOUT_MEDIUM });
    await expect(sendRow()).toHaveCount(1);
    await expect(row('for gas')).toHaveCount(1);

    await editSendNotes('Paid the supplier'); // kept for the login test below
  });

  test('excludes a transaction from accounting and deletes another', async () => {
    await group(SEND_TX.hash).locator('[data-testid=history-event-group-menu]').click();
    await ctx.sharedPage.getByRole('button', { name: 'Exclude from Accounting' }).click();
    await expect(group(SEND_TX.hash).locator('[data-testid=ignored-in-accounting]')).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });

    await group(RECEIVE_TX.hash).locator('[data-testid=history-event-group-menu]').click();
    await ctx.sharedPage.getByRole('button', { name: 'Delete transaction & events' }).click();
    await ctx.sharedPage.locator('[data-testid=confirm-dialog] [data-testid=button-confirm]').click();
    await expect(row('Receive')).toHaveCount(0, { timeout: TIMEOUT_MEDIUM });
  });

  test('keeps the changes after logging in again', async () => {
    // A logout through the menu races the refresh that follows the deletion. So the app is
    // unloaded first, the backend is logged out through the API, which closes the database, and
    // the app is loaded as a new document: from the app itself, visit() only changes the hash.
    await ctx.sharedPage.goto('about:blank');
    await apiLogout(ctx.sharedRequest);
    await ctx.app.visit();
    await ctx.app.login(ctx.username);
    await page.visit();
    await page.applyTableFilter('location', 'tron');

    await expect(row('Paid the supplier')).toHaveCount(1, { timeout: TIMEOUT_MEDIUM });
    await expect(group(SEND_TX.hash).locator('[data-testid=ignored-in-accounting]')).toHaveCount(1);
    await expect(row('Receive')).toHaveCount(0);
  });
});
