SELECT 
	journal.full_name,
    save_circuit(provenance(), 'journal_id', '{path}') 
FROM 
	author, organization, writes, publication, journal, domain_journal, domain 
WHERE
	author.oid = organization.oid
	AND author.aid = writes.aid
    AND writes.pid = publication.pid
    AND publication.jid = journal.jid
    AND journal.jid = domain_journal.jid
    AND domain_journal.did = domain.did
    AND organization.name = 'University of California San Diego'
    AND publication.year > 2010
GROUP BY journal.full_name 
LIMIT 10