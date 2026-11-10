{
  description = "Backport e1aa48f to the pinned Leios consensus";
  outputs = { self }: {
    haskellNix.modules = [{
      packages.ouroboros-consensus.patches = [ ./complete-ledger-config.patch ];
    }];
  };
}
